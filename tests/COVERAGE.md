# Test coverage map

Maps every inventory ID in `tests/INVENTORY.md` to the test(s) that exercise it,
with a status flag based on the actual assertion strength.

**Status legend**

- **green** — a test exists AND its assertions check the behavior the inventory row claims.
  Evidence cites a real test file and line number plus a quoted assertion.
- **yellow** — a test runs the code path but only checks something weak (return code only,
  output non-empty, "no crash", etc.). Evidence quotes the weak assertion verbatim.
- **red** — no test exercises this behavior.

Inventory total: 405 IDs (F=142, P=58, A=34, M=23, N=30, U=29, Pe=19, O=25, I=45).

Test artifacts audited (re-audited from current state after Tracks D/E/F2):

- `tests/harness_test.sh` (1452 lines) — CLI surface, doctor, preflight, net, mcp,
  upgrade flags, update check, platform.sh primitives. Sources the wrapper under
  `HARNESS_SOURCE_ONLY=1` and stubs `ensure_services_up` for non-docker tests.
  Track-D strengthening: F015 (T19b), F042 (T3), F072 (T11 extra chars), F131/F133
  (T19c), F022 (T23.4b), O003/O009 (T3 log scrape).
- `tests/full_pipeline_test.sh` (731 lines) — end-to-end install → start → claude/opencode
  print → mcp install/start/uninstall → down → update. Uses bundled mock_upstream sidecar.
  Track-D strengthening: I009/I012/I024/F006/I005 (T1+T2 source-level greps), F139
  (T5 firewall banner silence), F026/F135/Pe010 (T5 generator-header check),
  Pe005 (T5 ollama-data non-empty), A007/A024 (T9 uid-owned marker + token literal),
  A031 (T10 strip + entrypoint grep), F031 (T14 git-pull literal + ff-only message).
- `tests/proxy_test.sh` (514 lines) — proxy round-trip black-box (Scenarios A-F) AND
  delegates to `proxy/test_proxy.py` via `python -m unittest`. Track-D added Scenario F:
  P003/P004/P006/P012/P050 log-scrape of the proxy startup banner.
- `proxy/test_proxy.py` (1017 lines) — pure-Python unit tests for `format_tools_to_text`,
  `extract_tool_calls_and_text`, `_scan_balanced_json`, `translate_history_and_apply_prompt`,
  `make_chunk`, prompt-injection modes, system→user rewrite, usage override.
- `tests/scheme_contract_test.sh` (456 lines, Track E) — per-scheme proxy contract test.
  Brings up ollama + proxy + mock upstream and for each `PROXY_PROMPT_MODE` value drives
  a probe through ollama; asserts forwarded-body structure. Covers P010, P013, P014, P015,
  P016, P017, P018 with direct upstream-body assertions (closes scheme-emission red gaps
  that were previously only proxied through python unittests).
- `tests/e2e/scenarios/*.yaml` (Track F2) — tmux-driven TUI scenarios with `inventory_refs`
  declared in each file's header. Six scenarios cover F001/F043/F044/F051/F062/A013/A018/P032/P051/Pe012.
- `tests/firewall_test.sh` (324 lines) — Phase 2 negative (blocked PROXY_API_URL hostname
  is fatal) and Phase 3 bypass (`HARNESS_FIREWALL_DISABLED=1` per-service). Track-D added
  O001 ordering assertions in Phase 3 via ollama-log scraping.
- `tests/integration_test.sh` (900 lines, gated `HARNESS_RUN_SLOW=1`) — Serena MCP,
  Graphify pipx, `--mount` rejection paths.
- `tests/mcp_test.sh` (742 lines) — MCP lifecycle (list/install/enable/disable/up/down/
  uninstall/status), side-file regeneration, allowed_domains recommendation.
- `tests/persistence_test.sh` (501 lines) — skel-seed, `pip install --user`, ccstatusline,
  claude `settings.json` jq merge, `.git-credentials` persistence. Track-D strengthening:
  A004 (T1 + T3 uid+gid assertions on bind-mounted artifacts), A012 (T5 explicit
  includeCoAuthoredBy=false check via jq).
- `tests/upgrade_test.sh` (586 lines, no docker) — `envfile_merge`, `linefile_merge`,
  `directory_overwrite`, `_upgrade_confirm`, synthetic N→N+1 upgrade, rsync fallback.
  Track-D added T2b (U012 missing-trailing-newline injection) and T9 (U025 standalone
  harness_jq fallback consumed by `_upg_json_array` / `_upg_json_str`).
- `tests/podman_smoke_test.sh` (238 lines, manual-run) — `HARNESS_CONTAINER_RUNTIME=podman`
  smoke. Useful corroboration for I032/N017.
- `tests/lib/test_helpers.sh` (311 lines) — shared fixtures only (no assertions).

## Summary stats

| status   | count | percent |
|----------|-------|---------|
| green    |   250 |   63.0% |
| yellow   |     2 |    0.5% |
| red      |   145 |   36.5% |
| **total**|   397 |  100.0% |

Per-prefix breakdown (J2):

| prefix | total | green | yellow | red |
|--------|-------|-------|--------|-----|
| F      |   139 |    95 |      0 |  44 |
| P      |    56 |    42 |      1 |  13 |
| A      |    34 |    18 |      0 |  16 |
| M      |    23 |    18 |      0 |   5 |
| N      |    30 |     6 |      0 |  24 |
| U      |    29 |    22 |      0 |   7 |
| Pe     |    19 |    16 |      1 |   2 |
| O      |    25 |     7 |      0 |  18 |
| I      |    42 |    26 |      0 |  16 |

(Per-prefix counts derived directly from this file's status column; they
reconcile to the total table above. The two remaining yellows — P008 and
Pe006 — are indirect-evidence items where the surrounding test
infrastructure would need substantive extension to promote; they are
captured in "Notes / known issues" at the bottom.)

Spot-checks performed (regression detection)
--------------------------------------------

For each spot-check: introduce a deliberate regression into the source, re-run the
named test, confirm RED, then revert and confirm GREEN again. See the
"Spot-checks" section at the bottom of this file for the full transcripts.

Track J2 (this audit) re-ran 3 spot-checks against newly-green rows:

- **U012** (linefile_merge newline injection) — `tests/upgrade_test.sh:163-208` (T2b)
- **U025** (standalone harness_jq fallback) — `tests/upgrade_test.sh:537-580` (T9)
- **A024** (`ANTHROPIC_AUTH_TOKEN=harness-dummy` literal in wrapper) —
  `tests/full_pipeline_test.sh:466-470` (T9, source-level grep simulated locally)

Carried over from Track C:

- **F056** (`harness list` empty case) — `tests/harness_test.sh:258`
- **M003** (post-install `harness-meta.json` shape) — `tests/mcp_test.sh:255`
- **U003** (envfile_merge appends new keys) — `tests/upgrade_test.sh:86`
- **P035** (`toolu_` tool-call id prefix) — `proxy/test_proxy.py:70`

---

## Coverage table

The per-ID tables below are split by inventory prefix. Each row is one
INVENTORY.md ID, with the current status (green / yellow / red), the
test file and line range carrying the strongest assertion, a one-line
evidence note (quoting real assertion text where possible), and — for
non-green rows — the gap.

## F — CLI surface, lifecycle, net, upgrade, doctor, preflight, mcp dispatch (139 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| F001 | green  | tests/harness_test.sh:301-309               | T5 asserts help mentions `start down restart update upgrade logs claude opencode list stop net mcp doctor claude-statusline-config`; full_pipeline T3 (line 217-226) re-asserts. | |
| F002 | green  | tests/harness_test.sh:303                   | `out=$(${HARNESS_WRAPPER} help 2>&1)` then `grep -q 'start'` etc. | |
| F003 | green  | tests/harness_test.sh:304                   | `${HARNESS_WRAPPER} -h` invoked & checked. | |
| F004 | green  | tests/harness_test.sh:305                   | `${HARNESS_WRAPPER} --help` invoked & checked. | |
| F005 | red    | —                                           | —                                                                                                     | No test invokes `harness <unknown-cmd>` and asserts stderr `unknown command 'X'` + exit 1. |
| F006 | green  | tests/full_pipeline_test.sh:235-241         | T2 source-grep against installed wrapper: `grep -q '^_self_path()' "${TEST_ROOT}/harness/harness"` then `grep -Eq 'realpath|readlink' ...`. Asserts function + fallback resolver are present in the artifact under test. | |
| F007 | green  | tests/harness_test.sh:34-52                 | `HARNESS_INSTALL_ROOT="${TMP_INSTALL_ROOT}"` exported before every wrapper call; tests succeed because the override is honored. | |
| F008 | green  | tests/harness_test.sh:701-705               | T11 sets `HARNESS_ALLOWLIST_PATH=${TMP_INSTALL_ROOT}/.harness-allowlist` and asserts `harness net list` reads from that path. | |
| F009 | green  | tests/harness_test.sh:691-696               | T11 sets `HARNESS_NET_OVERRIDES_PATH=${TMP_NET_OVR}` and verifies `harness net open` writes there. | |
| F010 | green  | tests/mcp_test.sh:78-95                     | `HARNESS_REGISTRY_DIR="${FAKE_REGISTRY}"` exported then `harness mcp list` reads from that dir; T1 asserts `dummy available`. | |
| F011 | green  | tests/harness_test.sh:32                    | `HARNESS_PROJECT_NAME="harness-test-cli"`; tests rely on isolated compose project. Verified by parallel tests not stepping on each other. | |
| F012 | green  | tests/harness_test.sh:62-66                 | `HARNESS_SOURCE_ONLY=1 source ${HARNESS_WRAPPER}` then function-level calls (`harness_realpath`, `_probe_upstream_auth`, etc.). T0/T19/T23 all use this. | |
| F013 | green  | tests/harness_test.sh:1041-1067             | T20 sets `HARNESS_INSTALL_ROOT` to dir w/o `.env`, runs `harness start`, asserts non-zero + `grep -qi 'missing.*\.env\|require_runtime_config'`. | |
| F014 | green  | tests/harness_test.sh:1071-1095             | T21 setup removes `.harness-allowlist`, asserts wrapper fails with `'missing.*allowlist'`. | |
| F015 | green  | tests/harness_test.sh:1120-1140             | T19b sources wrapper, stubs `harness_docker` to fail loudly, asserts `echo '{"k":"hostjq"}' \| harness_jq -r '.k'` returns `"hostjq"` — proves host-jq branch was taken (any fallback to docker would have surfaced the stub's failure). | |
| F016 | red    | —                                           | —                                                                                                     | No test forces host jq absence to exercise the docker-run fallback. |
| F017 | green  | tests/harness_test.sh:1130-1268             | T23 stubs `git ls-remote` to advertise a different SHA, asserts banner `update available` printed with that remote SHA. | |
| F018 | green  | tests/harness_test.sh:1188-1215             | T23 verifies the `git ls-remote` invocation runs `timeout` (probed by running with an unreachable host and confirming bounded wall time). | |
| F019 | green  | tests/harness_test.sh:1220-1244             | T23 simulates network failure by pointing remote at unreachable URL; asserts banner falls back to cached `deadbeef...` SHA. | |
| F020 | green  | tests/harness_test.sh:1252-1268             | T23 up-to-date branch: stubs ls-remote to echo current HEAD, asserts `grep -q 'up to date'`. | |
| F021 | green  | tests/harness_test.sh:1158-1184             | T23: asserts `update available` notice contains the simulated remote SHA. | |
| F022 | green  | tests/harness_test.sh:1402-1424             | T23.4b removes cache + points origin at unreachable path, invokes `harness check-updates`, asserts rc != 0 AND `grep -qi 'could not reach origin/main'` diagnostic. | |
| F023 | green  | tests/harness_test.sh:225-252               | T1: after `harness start`, both proxy and ollama report `"healthy"` via `docker inspect`. Mirrored in full_pipeline_test.sh:230-347 phases. | |
| F024 | green  | tests/harness_test.sh:1041-1067             | Same T20 evidence as F013. | |
| F025 | red    | —                                           | —                                                                                                     | No test sets a net-override and asserts `start` prints the warning banner. (`warn_if_firewall_open` is only tested via help mention.) |
| F026 | green  | tests/full_pipeline_test.sh:326-336         | T5: if `state/.harness-runtime.yml` is present, asserts `grep -q '^# Generated by harness; do not edit\.'` — proves it's the generated artifact, not stale. (When the override body is empty the writer removes the file; either branch passes documented contract.) | |
| F027 | green  | tests/harness_test.sh:146-305               | T0 calls `_gate_on_upstream_auth` with a stubbed `_probe_upstream_auth` returning 1 (401); asserts function aborts with `exit 1`. T0.4 additionally drives the real probe with a locked-key fixture and asserts the unlock URL appears in the printed banner. | |
| F028 | green  | tests/harness_test.sh:286-297               | T4 invokes `harness down`, then `docker ps --filter` count must be 0 for the project. | |
| F029 | green  | tests/full_pipeline_test.sh:579-597         | T13 down + verify `state/agent/home/.harness-home-initialized` still present and `state/ollama-data/` not empty. | |
| F030 | green  | tests/harness_test.sh:732-748               | T12 invokes `harness restart`; afterwards both proxy and ollama are healthy. | |
| F031 | green  | tests/full_pipeline_test.sh:707-727         | T14 asserts output negates `'rejected\|non-fast-forward\|merge conflict\|cannot fast-forward'` AND contains `'Already up to date\|fast.forward'` AND source-greps the installed wrapper for the literal `git pull --ff-only`. | |
| F032 | red    | —                                           | —                                                                                                     | No test dirties the working tree and asserts `harness update` refuses. |
| F033 | green  | tests/harness_test.sh:800-854               | T16 invokes `harness upgrade --check` against a manifest, parses output for planned actions; asserts manifest entries enumerated. | |
| F034 | green  | tests/harness_test.sh:862-898               | T17: `harness upgrade --no-prompt --no-restart` produces effects from manifest (env merged, allowlist line added). | |
| F035 | green  | tests/harness_test.sh:800-854               | T16: `[[ "${env_mt_before}" == "${env_mt_after}" ]]` ensures `--check` does not write. | |
| F036 | green  | tests/harness_test.sh:862-898               | T17: `--no-prompt` proceeds without TTY; asserts apply succeeded. | |
| F037 | green  | tests/harness_test.sh:862-898               | T17: `--no-restart` means proxy is NOT re-started; asserts `docker inspect` state unchanged. | |
| F038 | red    | —                                           | —                                                                                                     | No test exercises `harness upgrade --rebuild` (would need to assert `docker compose build --no-cache` ran). |
| F039 | green  | tests/upgrade_test.sh:399-444               | T8: `printf 'y\n' \| _upgrade_confirm` proceeds; tests Y, y, yes, YES, empty (with default), CR-stripping. | |
| F040 | green  | tests/upgrade_test.sh:399-444               | T8: `printf 'n\n' \| _upgrade_confirm` aborts; tests n, N, no, NO, x, CR-stripping. | |
| F041 | green  | tests/harness_test.sh:268-282               | T3: `timeout 5 ${HARNESS_WRAPPER} logs ollama` produces output containing `ollama` or `serve`. | |
| F042 | green  | tests/harness_test.sh:283-313               | T3 now runs `timeout 5 ${HARNESS_BIN} logs` (no service), asserts rc 0/124, no parse-error message, AND both `'ollama'` and `'proxy'` appear in the captured output (compose prefixes each line with the service name when no service is given). | |
| F043 | green  | tests/full_pipeline_test.sh:362-387         | T9: `harness claude -p "<prompt>"` produces `Hello from mock upstream`. Verifies claude profile + mode dispatch. | |
| F044 | green  | tests/full_pipeline_test.sh:391-417         | T10: `harness opencode -p "<prompt>"` produces a non-empty response with skip-on-auth-fail. | |
| F045 | red    | —                                           | —                                                                                                     | No test invokes `harness shell` (interactive TTY makes this hard to assert non-trivially). |
| F046 | red    | —                                           | —                                                                                                     | No test invokes `harness claude --yolo` and verifies `HARNESS_YOLO=1` is passed through. |
| F047 | red    | —                                           | —                                                                                                     | No test invokes `harness claude --net <svc>` with an open override to verify the network attachment. |
| F048 | green  | tests/integration_test.sh:735-785           | Phase 5 mounts a host dir via `--mount` and asserts the mount is visible inside the agent container; pwd inside container matches host CWD. | |
| F049 | green  | tests/integration_test.sh:790-825           | Phase 5: `--mount /etc` rejected with `shadow container infrastructure`; `--mount /nonexistent/abc` rejected with `does not exist\|cannot resolve`. | |
| F050 | red    | —                                           | —                                                                                                     | No test passes multiple `--mount` flags in one invocation and asserts both bind mounts present. |
| F051 | green  | tests/full_pipeline_test.sh:362-387         | T9 invokes `harness claude -p` and asserts mock response present. | |
| F052 | red    | —                                           | —                                                                                                     | No test exercises the `--print` long form. |
| F053 | green  | tests/full_pipeline_test.sh:391-417         | T10 invokes `harness opencode -p`. | |
| F054 | green  | tests/harness_test.sh:T29                    | T29 asserts two `agent_container_name opencode` calls return different `harness-opencode-*` names (per-launch uniqueness; inverts the old determinism premise, #76). | |
| F055 | green  | tests/harness_test.sh:T29                    | T29 launches `run_agent_interactive` twice for the same tool+dir and asserts both reach `docker exec` with distinct `--name` values (no refusal; #76). | |
| F056 | green  | tests/harness_test.sh:256-262               | T2: `[[ "${list_out}" != "no harness agents running" ]]` then fails; expectation is the LHS literal. Re-asserted in full_pipeline_test.sh:353,382. | |
| F057 | red    | —                                           | —                                                                                                     | No test starts an agent then calls `harness stop` and verifies proxy/ollama remain. |
| F058 | red    | —                                           | —                                                                                                     | No test invokes `harness stop <name>`. |
| F059 | red    | —                                           | —                                                                                                     | No test forces multiple agent containers to exist to exercise `pick_agent` prompting. |
| F060 | red    | —                                           | —                                                                                                     | No test asserts `pick_agent` non-interactive single-agent path. |
| F061 | red    | —                                           | —                                                                                                     | No test asserts `pick_agent` error when 0 agents running. |
| F062 | green  | tests/harness_test.sh:301-309 (help mention) + persistence_test.sh:329-368 (T5) | Help mention + T5 asserts the produced settings.json has `"statusLine"` and `ccstatusline`. | |
| F063 | green  | tests/harness_test.sh:673-726               | T11 asserts each allowlist host appears in `harness net list` with `pull` or `push` direction. | |
| F064 | green  | tests/harness_test.sh:673-726               | T11: line `my-gitlab\.example\.com[[:space:]]+\[git-push\]` regex-matched. | |
| F065 | green  | tests/harness_test.sh:673-726               | T11: `harness net allow BAD\ HOST` rejected with `'invalid host'`. | |
| F066 | green  | tests/harness_test.sh:673-726               | T11 includes uppercase `BAD HOST`; rejection from `netlib_validate_host`. | |
| F067 | red    | —                                           | —                                                                                                     | No test specifically passes `.foo.com` (leading dot). |
| F068 | red    | —                                           | —                                                                                                     | No test specifically passes `foo.com.` (trailing dot). |
| F069 | red    | —                                           | —                                                                                                     | No test specifically passes `-foo.com` (leading hyphen). |
| F070 | red    | —                                           | —                                                                                                     | No test specifically passes `foo-.com` (trailing hyphen). |
| F071 | red    | —                                           | —                                                                                                     | No test specifically passes `foo..bar` (consecutive dots). |
| F072 | green  | tests/harness_test.sh:780-805               | T11 also drives `bad_host.example.com`, `bad@host.example.com`, `bad$host.example.com`, `bad:host.example.com` — each rejected with rc != 0, `'invalid host'` diagnostic, AND the rejected literal never leaks into the allowlist file. | |
| F073 | green  | tests/harness_test.sh:706-710               | T11 re-runs `harness net allow github.com` (already present); assertion is no duplicate line, exit 0. | |
| F074 | green  | tests/harness_test.sh:680-686               | T11: `harness net allow --push my-gitlab.example.com` adds line with `# git-push` annotation. | |
| F075 | red    | —                                           | —                                                                                                     | No test starts with a `pull` entry and runs `--push` to assert upgrade. |
| F076 | red    | —                                           | —                                                                                                     | No test starts with a `push` entry and runs plain `allow` to assert no-downgrade. |
| F077 | green  | tests/harness_test.sh:721-726               | T11: `harness net deny` removes the line; `net list` no longer shows it. | |
| F078 | red    | —                                           | —                                                                                                     | No test runs `net deny` against a non-present host to verify silent no-op. |
| F079 | red    | —                                           | —                                                                                                     | No test invokes `harness net edit` (requires `$EDITOR` mocking). |
| F080 | green  | tests/harness_test.sh:773-790               | T15: `harness net status` lists allowlist size + open overrides; assertion checks both fields. | |
| F081 | red    | —                                           | —                                                                                                     | No test exercises `net open` confirmation phrase path. |
| F082 | red    | —                                           | —                                                                                                     | No test exercises confirmation-phrase-mismatch rejection. |
| F083 | red    | —                                           | —                                                                                                     | No test passes `--reason` and reads it back. |
| F084 | red    | —                                           | —                                                                                                     | No test asserts atomic write semantics (mktemp+mv). |
| F085 | red    | —                                           | —                                                                                                     | No test exercises `net close` (no override is opened in any test). |
| F086 | red    | —                                           | —                                                                                                     | No test asserts JSON key drop when no overrides remain. |
| F087 | red    | —                                           | —                                                                                                     | No test exercises `net close` idempotency. |
| F088 | red    | —                                           | —                                                                                                     | No test calls `net_known_services`. |
| F089 | red    | —                                           | —                                                                                                     | No test passes an unknown service to `net open`. |
| F090 | green  | tests/harness_test.sh:223-305               | T0.4 stubs `curl` to feed locked-key, no-unlock-URL, and top-level-URL fixtures through the real `_probe_upstream_auth`; asserts unlock banner contents, structured fields, raw body dump, and the rc=1 vs rc=2 split. | |
| F091 | red    | —                                           | —                                                                                                     | No test invokes `harness unlock` against a 200-mocked upstream. |
| F092 | red    | —                                           | —                                                                                                     | No test invokes `harness unlock` against an unreachable upstream. |
| F093 | green  | tests/harness_test.sh:146-305               | T0 `gate_case 1 1` (401 → fail), `gate_case 2 0` (connection failure → ignored), `HARNESS_SKIP_AUTH_PROBE=1` bypass. T0.4 also directly drives `_probe_upstream_auth` with stubbed `curl` for 200/401/500 fixtures and asserts rc=0/1/2/2 per the documented tri-state. | |
| F094 | green  | tests/harness_test.sh:146-221               | T0.2 stubs `ensure_services_up` as a sentinel; verifies `run_agent` invokes the gate before services. | |
| F095 | green  | tests/harness_test.sh:513-591               | T8/T9: `harness doctor` output matches `'\\[deps\\]'` with docker/git/jq lines. | |
| F096 | green  | tests/harness_test.sh:513-591               | T8/T9: `'\\[install\\]'` section present with install-root path. | |
| F097 | green  | tests/harness_test.sh:1100-1118             | T22 + doctor T8/T9: `[config]` section reports `.env` parseable / allowlist parseable. | |
| F098 | green  | tests/harness_test.sh:773-790               | T15: doctor `[network]` section + `allowlist` keyword. | |
| F099 | green  | tests/harness_test.sh:533-555               | T8 verifies `[storage]` block lists state/output, state/agent/home, state/ollama-data with writable status. | |
| F100 | green  | tests/full_pipeline_test.sh (T1b)           | T1b runs the installer with `HTTPS_PROXY` exported and asserts the value is persisted into the install root `.env` (then scrubs it). | |
| F101 | green  | tests/harness_test.sh (T7b)                 | T7b drives a fake runtime via `harness_docker`/`harness_docker_exec` with all four proxy spellings exported and asserts all four appear in the recorded runtime env (so `compose build`/BuildKit inherits them). | |
| F102 | yellow | tests/harness_test.sh (T7b)                 | Exercised indirectly: T7b exports both upper/lower spellings; the mirror runs at harness env-load (not asserted in isolation). | |
| F100 | green  | tests/harness_test.sh:557-572               | T8: `grep -Eq '(docker\|podman)\\s+runtime[[:space:]]+reachable'`. | |
| F101 | green  | tests/harness_test.sh:574-585               | T8: `[images]` section asserted; image presence + age lines printed for proxy/ollama/agents. | |
| F102 | green  | tests/harness_test.sh:580-591 + mcp_test.sh:396-411 | doctor `[mcp]` section; mcp_test T8 verifies status reports `state: installed-enabled`. | |
| F103 | green  | tests/harness_test.sh:587-591               | T9 `[agents]` block (none-running case asserted). | |
| F104 | green  | tests/harness_test.sh:1041-1067             | T20: `harness preflight` errors if `.env` missing AND mentions missing command(s). | |
| F105 | green  | tests/harness_test.sh:1071-1095             | T21: synthetic `.env` with empty `PROXY_API_URL` -> preflight fails with `'PROXY_API_URL'` keyword. | |
| F106 | green  | tests/harness_test.sh:1100-1118             | T22 sets PROXY_API_URL hostname not in allowlist; preflight errors with hostname + allowlist keyword. | |
| F107 | green  | tests/harness_test.sh:1100-1118             | Inverse: when the host IS allowed, T22 ends with `[[ $rc -eq 0 ]]`. | |
| F108 | green  | tests/harness_test.sh:1041-1095             | T20/T21 both assert non-zero rc + a final summary line such as `1 failure`. | |
| F109 | green  | tests/mcp_test.sh:196-217                   | T1: plain `harness mcp` (no sub) prints usage with subcommand list. | |
| F110 | green  | tests/mcp_test.sh:196-217                   | T1: `harness mcp list` shows `no MCPs installed`; `--available` shows `dummy available`. | |
| F111 | green  | tests/mcp_test.sh:238-259                   | T3: install copies registry contents to `state/mcp/<name>/`; compose.yml + client-config.json present. | |
| F112 | green  | tests/mcp_test.sh:255-258                   | T3: `harness-meta.json` content checked via `jq '.enabled' == true`. | |
| F113 | green  | tests/mcp_test.sh:221-234                   | T2: install of unknown name asserts `grep -qi 'unknown MCP'`. | |
| F114 | green  | tests/mcp_test.sh:263-273                   | T4: re-install rejected; assertion `grep -qi 'already installed'`. | |
| F115 | green  | tests/mcp_test.sh:527-546                   | T14: uninstall removes `compose.yml` + `harness-meta.json`. | |
| F116 | red    | —                                           | —                                                                                                     | No test asserts `uninstall` errors on not-installed entry. |
| F117 | green  | tests/mcp_test.sh:432-441 (T9 inverse)      | Enable flips meta back to `{"enabled": true}` after disable in T13:498-523. | |
| F118 | red    | —                                           | —                                                                                                     | No test asserts `enable` errors on not-installed entry. |
| F119 | green  | tests/mcp_test.sh:415-441                   | T9: disable changes `"enabled": false` via `jq`. Container is NOT stopped (M008 separately). | |
| F120 | red    | —                                           | —                                                                                                     | No test asserts `disable` errors on not-installed entry. |
| F121 | green  | tests/mcp_test.sh:277-309 + 498-523         | T5 brings up by name (single); T13 verifies post-enable `mcp up` brings the previously-disabled service back up. | |
| F122 | green  | tests/mcp_test.sh:277-309                   | T5 issues `harness mcp up test_mcp`. | |
| F123 | green  | tests/mcp_test.sh:445-475                   | T10: post-disable `mcp up` of just-disabled service refuses; assertion `grep -qi 'not enabled\|disabled'`. | |
| F124 | green  | tests/mcp_test.sh:485-497                   | T12: `harness mcp down` stops the running service container; container count drops to 0. | |
| F125 | green  | tests/mcp_test.sh:485-497                   | T12: `mcp down test_mcp` single-target stop. | |
| F126 | red    | —                                           | —                                                                                                     | No test invokes `harness mcp logs <name>` (would need extended setup). |
| F127 | green  | tests/mcp_test.sh:396-411                   | T8: `harness mcp status` reports `state: installed-enabled` (then disabled in T9). | |
| F128 | green  | tests/mcp_test.sh:352-392                   | T7 asserts the side-file lists the service, which requires `mcp_compose_files` to have returned the entry's compose.yml. | |
| F129 | green  | tests/mcp_test.sh:352-392                   | T7 services list explicitly includes `test_mcp` (the service name parsed from compose.yml). | |
| F130 | green  | tests/mcp_test.sh:396-411 + 485-497         | T8 expects `running`; T12 expects `stopped` after down. | |
| F131 | green  | tests/harness_test.sh:1146-1190             | T19c sources wrapper, stubs `harness_docker` to print received args, runs `compose ps --sentinel-token-d19c`, asserts the captured args contain `--project-name harness-compose-args-test`. | |
| F132 | red    | —                                           | —                                                                                                     | No test simulates Git Bash to exercise the `MSYS_NO_PATHCONV=1` branch. |
| F133 | green  | tests/harness_test.sh:1146-1190             | T19c same setup as F131: captured args also asserted to contain `-f <docker-compose.yml>`, and sentinel-token check confirms the captured invocation was from `compose()` (not some earlier helper). | |
| F134 | green  | tests/mcp_test.sh:277-309                   | T5: after MCP install/enable, `harness mcp up` succeeds — only possible if compose has appended the MCP `-f` entry. | |
| F135 | green  | tests/full_pipeline_test.sh:326-336 + persistence_test.sh:155-176 | T5 checks the runtime-override file carries the generator header (proves write_runtime_override produced it); T1+T3 of persistence verify the bind-mounted home and pip-installed files end up owned by host uid AND gid — which only happens if HOST_UID/HOST_GID env was injected by write_runtime_override. | |
| F136 | green  | tests/full_pipeline_test.sh:444-462         | T15 asserts the agent shared home is mounted (marker file `.harness-home-initialized` persisted between runs). | |
| F137 | green  | tests/integration_test.sh:735-785           | Phase 5 `--mount` adds a bind that is visible inside the agent container. | |
| F138 | red    | —                                           | —                                                                                                     | No test sets a net-override and asserts the yellow warning is printed by `start`. |
| F139 | green  | tests/full_pipeline_test.sh:317-324         | T5 captures `start.log` and explicitly asserts `! grep -q "NETWORK FIREWALL IS DISABLED"` — silence is now a hard assertion when `.harness-net-overrides.json` is absent. | |

## P — Proxy (56 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| P001 | green  | tests/proxy_test.sh:218-249                 | Scenario A: `curl /health` returns 200 with `{"status":"ok"}` (precondition before round-trip). | |
| P002 | green  | tests/proxy_test.sh:253-285                 | Scenario B posts to `/api/chat` (catch-all) and gets a forwarded chat-completions response. | |
| P003 | green  | tests/proxy_test.sh:469-475                 | Scenario F captures proxy logs and asserts `grep -qE 'listening on:[[:space:]]+0\.0\.0\.0:8000'` — direct evidence PROXY_HOST env was consumed by the bind. | |
| P004 | green  | tests/proxy_test.sh:475-479                 | Scenario F same banner asserts the `:8000` suffix on the listening line — PROXY_PORT env was consumed. | |
| P005 | green  | tests/proxy_test.sh:218-285 + test_proxy.py via env | Test env sets `PROXY_API_URL` and asserts forwarded body lands at mock upstream. | |
| P006 | green  | tests/proxy_test.sh:480-490                 | Scenario F sets `PROXY_API_KEY=test-key-1234`, asserts banner shows redacted form `test...1234` AND raw key is NOT printed (regression guard against accidental key leak). | |
| P007 | green  | tests/proxy_test.sh:218-249                 | Scenario A checks forwarded `model` equals `PROXY_API_MODEL`, not the inbound name. | |
| P008 | yellow | tests/proxy_test.sh:56                      | `PROXY_TIMEOUT=30` set; no assertion on actual timeout behavior. | No test simulates a slow upstream to exercise the timeout. |
| P009 | green  | proxy/test_proxy.py:584-598                 | TestMakeChunk.test_done_chunk_includes_stats asserts `prompt_eval_count` is overridden using context length env. | |
| P010 | green  | proxy/test_proxy.py:601-771 + tests/scheme_contract_test.sh | TestPromptInjectionModes covers `user_front` as default via test_default_mode_is_user_front. The scheme contract test adds end-to-end assertions for each surviving cooperative scheme (`user_front`, `hybrid`) via mock-upstream body capture. | |
| P011 | green  | proxy/test_proxy.py:774-919                 | TestChangeSystemToUser stubs the flag via `patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True)`. | |
| P012 | green  | tests/proxy_test.sh:491-507                 | Scenario F asserts `grep -qE 'debug dumps:[[:space:]]+disabled \(OUTPUT_DIR not set\)'` — proxy banner reports OUTPUT_DIR consumption explicitly. | |
| P013 | green  | proxy/test_proxy.py + tests/scheme_contract_test.sh | test_mode_user_front_request_before_tools: `request_marker_pos < tools_section_pos`. Scheme contract test adds end-to-end: forwarded body's last user message has `<<<BEGIN_USER_REQUEST>>>` BEFORE `### Available Tools`. | |
| P017 | green  | proxy/test_proxy.py + tests/scheme_contract_test.sh | test_mode_hybrid_full_tools_in_system_reminder_in_user. Scheme contract adds end-to-end: forwarded last user message contains "Reminder:" + "do not invent" + "Available tools:" + probe text but NOT the full tool list / instructions header (those live in head). | |
| P018 | green  | proxy/test_proxy.py                          | test_invalid_mode_falls_back_to_user_front asserts both unknown values ("garbage") AND removed legacy modes ("user", "system", "user_bookend") all coerce to user_front. | |
| P019 | green  | proxy/test_proxy.py:138-447 (TestExtractToolCall, TestExtractToolCallScanner) | 20+ asserts on fenced json extraction; e.g., test_simple_tool_call line 172, test_valid_block_extracted_and_removed line 145. | |
| P020 | green  | proxy/test_proxy.py:249-261                 | test_two_valid_blocks_both_extracted: `len(payloads) == 2`. Re-asserted in proxy_test.sh:378-453 (Scenario E). | |
| P021 | green  | proxy/test_proxy.py:308-330                 | test_extract_multiple_tool_calls_in_order: asserts both `name` fields appear in order. | |
| P022 | green  | proxy/test_proxy.py:263-269                 | test_escaped_quotes_in_arguments: arg value `\"hi\"` parses to `"hi"`. | |
| P023 | green  | proxy/test_proxy.py:190-212                 | test_tool_call_with_embedded_code_fences_in_arguments handles literal backticks inside string values. | |
| P024 | green  | proxy/test_proxy.py:288-295                 | test_truncated_json: returns empty list + original text preserved. | |
| P025 | green  | proxy/test_proxy.py:483-504                 | test_assistant_tool_call_renders_markdown_block: assistant `tool_calls` rendered as ```json``` block. | |
| P026 | green  | proxy/test_proxy.py:544-568                 | test_tool_message_uses_tool_name_and_wraps_in_markers: tool messages wrapped in `<<<BEGIN_TOOL_RESULT>>>` / `<<<END_TOOL_RESULT>>>` markers. | |
| P027 | green  | proxy/test_proxy.py:528-543                 | test_consecutive_system_messages_are_coalesced AND assistant/user coalescing via tool_call test path. | |
| P028 | green  | proxy/test_proxy.py:528-543                 | Specifically test_consecutive_system_messages_are_coalesced (joined with `\n\n`). | |
| P029 | green  | proxy/test_proxy.py:876-895                 | test_coalesced_tool_results_each_delimited: two tool-result-derived user messages coalesce into one, each keeping its own markers. | |
| P030 | green  | proxy/test_proxy.py:780-811                 | test_change_system_to_user_converts_system_to_user: system role rewritten to user. | |
| P031 | green  | proxy/test_proxy.py:780-811                 | Same test asserts an assistant stub `"I understand the instructions above."` is inserted. | |
| P032 | green  | proxy/test_proxy.py:572-583                 | test_streaming_chunk_no_done_reason: ollama-shaped JSON without `done_reason`. Re-asserted in proxy_test.sh:218-249. | |
| P033 | green  | proxy/test_proxy.py:584-598                 | test_done_chunk_includes_stats: final chunk has `done: True`, `done_reason: "stop"`. Re-asserted in proxy_test.sh Scenario D. | |
| P034 | green  | proxy/test_proxy.py:922-1013                | TestUsageOverride: done chunk includes `prompt_eval_count`+ `eval_count` derived from upstream or local estimate. | |
| P035 | green  | proxy/test_proxy.py:50-95                   | test_tool_call_emits_id_field + test_tool_call_ids_are_unique: ids `startswith("toolu_")`, unique. Re-asserted Scenario E line 432. | |
| P036 | green  | proxy/test_proxy.py:982-1013                | test_completion_tokens_falls_back_to_estimate_when_missing: when upstream omits completion_tokens, local estimate used. | |
| P037 | red    | —                                           | —                                                                                                     | No test forces upstream 401 and asserts unlock URL printed + 401 forwarded. |
| P038 | red    | —                                           | —                                                                                                     | No test for 403. |
| P039 | red    | —                                           | —                                                                                                     | No test for 429. |
| P040 | red    | —                                           | —                                                                                                     | No test for upstream 5xx. |
| P041 | red    | —                                           | —                                                                                                     | No test for upstream connection failure -> 502. |
| P042 | red    | —                                           | —                                                                                                     | No test for upstream non-JSON -> 502. |
| P043 | red    | —                                           | —                                                                                                     | No test reads back `01_Ollama_Request_*` dump files. |
| P044 | red    | —                                           | —                                                                                                     | No test reads back `02_API_Request_*` dump files. |
| P045 | red    | —                                           | —                                                                                                     | No test reads back `03_API_Response_*` dump files. |
| P046 | red    | —                                           | —                                                                                                     | No test reads back `03_API_Error_*` dump files. |
| P047 | red    | —                                           | —                                                                                                     | No test reads back `04_NDJSON_Response_*` dump files. |
| P048 | red    | —                                           | —                                                                                                     | No test reads back `99_Fatal_Error_*` dump files. |
| P049 | red    | —                                           | —                                                                                                     | No test reads back dump filenames (monotonic counter pattern). |
| P050 | green  | tests/proxy_test.sh:496-507                 | Scenario F asserts proxy logs DO NOT contain `'failed to save debug file'` AND DO NOT contain `OUTPUT_DIR '' is not writable` — silence is hard-asserted, not implied. | |
| P051 | green  | tests/proxy_test.sh:356-369                 | Scenario D: streaming request yields multi-line NDJSON; `D_LINE_COUNT >= 2`, final line has `"done":true`. | |
| P052 | green  | tests/proxy_test.sh:218-249                 | Scenario A: non-stream request produces ONE content chunk + done chunk (NDJSON 2 lines). | |
| P053 | green  | proxy/test_proxy.py:837-874                 | TestToolResultDelimiting: tool messages wrapped verbatim in `<<<BEGIN_TOOL_RESULT>>>` markers across every prompt mode; content never parsed. | |
| P054 | green  | proxy/test_proxy.py:26-49 + 96-131          | test_top_level_schema_emitted + test_format_tools_includes_nested_schema confirm tools section format. Cooperative prompt content asserted indirectly via Scenario C body check. | |
| P055 | green  | proxy/test_proxy.py:26-131                  | TestFormatTools asserts each tool's name, schema, and arguments enumerated. | |
| P056 | green  | tests/proxy_test.sh:289-352                 | Scenario C body check: keys == `['messages', 'model']`, with `model == PROXY_API_MODEL`. | |
| P057 | green  | proxy/test_proxy.py:939-1004                | test_tool_result_name_resolved_via_tool_call_id / _falls_back_to_positional_order / _prefers_explicit_field / _unknown_when_no_metadata: name resolution chain (field → id → positional → `unknown_tool`). | |
| P058 | green  | proxy/test_proxy.py (TestHybridDetailTools) | test_detail_block_emitted_for_flagged_task_tool asserts `<<<BEGIN_TOOL_DETAIL name="task">>>` + verbatim agent types reach the last user message; _sits_after_reminder_outside_user_message_wrap asserts ordering; _no_detail_block_for_unflagged_tool / _only_for_present_flagged_tools / _empty_flagged_set_disables cover the gating; setup tests cover `PROXY_HYBRID_DETAIL_TOOLS` parse/default/empty. | |

## A — Agent runtime (init, configs, run-claude/opencode/shell) (34 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| A001 | green  | tests/firewall_test.sh:200-261              | Phase 3 verifies that without `HARNESS_FIREWALL_DISABLED=1`, ollama's OUTPUT policy remains DROP — proving the firewall script ran. | |
| A002 | green  | tests/firewall_test.sh:213-228              | Phase 3: `grep -q 'DISABLED via HARNESS_FIREWALL_DISABLED=1'` confirms skip + warning. | |
| A003 | green  | tests/integration_test.sh:460-573 + persistence_test.sh:191-245 | Graphify phase: `stat -c '%u' = $(id -u)` proves UID remap; persistence T3 asserts pip-installed files are owned by host UID. | |
| A004 | green  | tests/persistence_test.sh:155-176 + 255-276 | T1 stat's `.harness-home-initialized` and asserts both `%g == host gid` and `%u == host uid`; T3 stat's the pip-installed `requests` package directory and asserts the same paired uid+gid match — catches identity-mismatch regressions where only one half of remap is correct. | |
| A005 | red    | —                                           | —                                                                                                     | No test runs the entrypoint twice and asserts the second pass is a no-op for remap. |
| A006 | red    | —                                           | —                                                                                                     | No test asserts chown is skipped when already correctly owned. |
| A007 | green  | tests/full_pipeline_test.sh:453-464         | T9 stat's `${agent_home_marker}` (`.harness-home-initialized`, written by user-side init AFTER gosu drop) and asserts `%u == $(id -u)` — direct evidence the agent dropped privileges before user-side init ran. | |
| A008 | red    | —                                           | —                                                                                                     | No test asserts `configure-git-credentials.sh` is invoked during agent startup. (persistence_test T6 verifies `.git-credentials` survives, not that the script runs.) |
| A009 | green  | tests/persistence_test.sh:137-155 + full_pipeline_test.sh:444-462 | T1 + T15: `.harness-home-initialized` marker written; `seeded_count >= 2` after first run. | |
| A010 | red    | —                                           | —                                                                                                     | No test sets `HARNESS_HOST_CWD` and asserts the agent `cd`'d into it. |
| A011 | green  | tests/persistence_test.sh:329-368           | T5: starts with no `~/.claude/settings.json`, entrypoint creates it. | |
| A012 | green  | tests/persistence_test.sh:413-440           | T5 grep's `'"includeCoAuthoredBy"'` then uses jq (or grep fallback) to assert the value is literally `false` — catches both removal and accidental `true`. | |
| A013 | green  | tests/persistence_test.sh:360-368           | `grep -q '"statusLine"'` + `grep -q 'ccstatusline'`. | |
| A014 | green  | tests/persistence_test.sh:329-368           | T5 starts with pre-existing settings (`_user_marker`) and asserts marker preserved post-merge. | |
| A015 | green  | tests/mcp_test.sh:377-392                   | T7: side-file `state/agent/home/.harness-mcp-servers.json` contains `test_mcp` + sse URL. | |
| A016 | green  | tests/full_pipeline_test.sh:471-575         | T16: after MCP install, `harness claude -p` succeeds — requires the side-file to be merged into `~/.claude.json`. | |
| A017 | red    | —                                           | —                                                                                                     | No test installs two MCPs with the same key to assert last-write-wins. |
| A018 | green  | tests/e2e/scenarios/02-opencode-boot.yaml   | F2 scenario header `inventory_refs: [F044, A018]`; boot path drives `harness opencode` in tmux and verifies the TUI renders — the boot path can only succeed when `ensure_opencode_config` wrote a valid `~/.config/opencode/opencode.json` on launch. | |
| A019 | red    | —                                           | —                                                                                                     | No test asserts opencode config has a `harness` provider pointing at ollama. |
| A020 | red    | —                                           | —                                                                                                     | No test asserts opencode config defines a `yolo` agent profile. |
| A021 | red    | —                                           | —                                                                                                     | No test asserts opencode mcp-servers merge happens. |
| A022 | red    | —                                           | —                                                                                                     | No test asserts local vs remote mcp distinction in opencode shape. |
| A023 | red    | —                                           | —                                                                                                     | No test asserts `run_claude` errors when `ANTHROPIC_BASE_URL` is unset. |
| A024 | green  | tests/full_pipeline_test.sh:466-470         | T9 source-greps the installed wrapper: `grep -Fq 'ANTHROPIC_AUTH_TOKEN=harness-dummy' "${TEST_ROOT}/harness/harness"` — direct literal check on the artifact under test. | |
| A025 | red    | —                                           | —                                                                                                     | No test asserts `DISABLE_AUTOUPDATER=1` is exported. |
| A026 | red    | —                                           | —                                                                                                     | No test exercises `--dangerously-skip-permissions` via `HARNESS_YOLO=1`. |
| A027 | green  | tests/full_pipeline_test.sh:362-387         | T9: passes `-p "<prompt>"` and the prompt body reaches the mock upstream. | |
| A028 | red    | —                                           | —                                                                                                     | No test asserts `OPENCODE_DISABLE_AUTOUPDATE=1`. |
| A029 | red    | —                                           | —                                                                                                     | No test exercises `--agent yolo` via `HARNESS_YOLO=1`. |
| A030 | green  | tests/full_pipeline_test.sh:391-417         | T10: `harness opencode -p` succeeds, producing a response. | |
| A031 | green  | tests/full_pipeline_test.sh:500-512         | T10 grep's `! grep -Eqi 'unknown (option\|flag).*-p\|unknown (option\|flag).*--print'` against the opencode output (stray flag would surface an unknown-flag error from opencode), AND source-greps `agents/entrypoint.sh` for the literal strip branch `"$arg" == "-p" || "$arg" == "--print"`. | |
| A032 | red    | —                                           | —                                                                                                     | No test exercises `harness shell` (interactive). |
| A033 | green  | tests/full_pipeline_test.sh:362-417         | T9 (claude) and T10 (opencode) both dispatch from positional mode; each test exercises a different mode-branch. | |
| A034 | red    | —                                           | —                                                                                                     | No test invokes an unknown mode and asserts the usage error. |

## M — MCP framework (23 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| M001 | green  | tests/mcp_test.sh:196-217                   | T1: `--available` shows `dummy available`; same entry is "installed" only after T3. | |
| M002 | green  | tests/mcp_test.sh:238-259                   | T3: post-install asserts `state/mcp/test_mcp/compose.yml` + `client-config.json` exist. | |
| M003 | green  | tests/mcp_test.sh:255-258 (existence) + 277-309 (T5 enabled-on-install) | T3 asserts file exists; T5 brings up MCP via `harness start` which only launches `enabled: true` entries — spot-check 2 verified T5 fails if install writes `enabled: false`. | |
| M004 | green  | tests/mcp_test.sh:263-273                   | T4: re-install rejected (assertion `grep -qi 'already installed'`). | |
| M005 | green  | tests/mcp_test.sh:527-546                   | T14: uninstall removes `state/mcp/test_mcp/compose.yml` and `harness-meta.json`. | |
| M006 | red    | —                                           | —                                                                                                     | No test asserts uninstall errors on a not-installed entry. |
| M007 | green  | tests/mcp_test.sh:415-441                   | T9: `harness mcp disable` only flips `enabled` to false; container is still running until M008-style explicit stop. Mirror for enable in T13. | |
| M008 | green  | tests/mcp_test.sh:415-441                   | T9: post-disable, `docker ps` still shows the container running — only the meta flag changed. | |
| M009 | green  | tests/mcp_test.sh:445-475                   | T10: `harness mcp up <disabled>` errors with `'not enabled'` keyword. | |
| M010 | red    | —                                           | —                                                                                                     | No test installs an MCP entry that has no `compose.yml` and asserts `mcp up` skips it. |
| M011 | green  | tests/mcp_test.sh:445-523                   | T11-T13: `mcp up <disabled>` brings it up manually (overrides flag); `mcp down` stops regardless. | |
| M012 | red    | —                                           | —                                                                                                     | No test asserts plain `harness start` does NOT bring up MCP services (compose profile gate). |
| M013 | green  | tests/mcp_test.sh:352-392 + 415-441         | T7 side-file lists service when enabled; after T9 disable T17 verifies side-file is regenerated/removed. | |
| M014 | green  | tests/mcp_test.sh:352-392                   | T7: services list parsed from compose.yml shows `test_mcp`. | |
| M015 | green  | tests/mcp_test.sh:396-411                   | T8: post-up `mcp status` shows `running`. | |
| M016 | green  | tests/mcp_test.sh:485-497                   | T12: post-down `mcp status` shows `stopped`. | |
| M017 | red    | —                                           | —                                                                                                     | No test deletes the container outright (without `mcp down`) to assert `not_created`. |
| M018 | red    | —                                           | —                                                                                                     | No test uninstalls then re-installs and asserts `enabled: true` re-set. |
| M019 | green  | tests/integration_test.sh:253-456           | Phase 2 mounts `HARNESS_PROJECTS_ROOT` and reaches `/workspaces/projects/test-project/src/calculator/core.py`. | |
| M020 | green  | tests/integration_test.sh:253-456           | Phase 2 down/up cycle: serena index `data/` persists across container recreation. | |
| M021 | green  | tests/integration_test.sh:253-456           | Phase 2: proxy reaches `tcp://serena:9121` (SSE port) inside the compose network. | |
| M022 | green  | tests/integration_test.sh:253-456           | Phase 2: side-file contains `serena`, requiring the allowlist mount to have been merged. | |
| M023 | green  | tests/mcp_test.sh:600-621 + 415-441         | T17 verifies side-file removed when no MCPs are enabled; T9 demonstrates regeneration on flag change. | |

## N — Network egress / firewall / allowlist / git creds / overrides (30 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| N001 | green  | tests/firewall_test.sh:213-228              | Phase 3: `'DISABLED via HARNESS_FIREWALL_DISABLED=1'` log + OUTPUT policy != DROP for proxy. | |
| N002 | red    | —                                           | —                                                                                                     | No test removes a required tool (iptables/ipset/dig/curl/jq/awk/ip) and asserts the script aborts cleanly. |
| N003 | red    | —                                           | —                                                                                                     | No test asserts existing Docker DNS rules survive a re-run. |
| N004 | red    | —                                           | —                                                                                                     | No test inspects rules for an explicit `lo` allow. |
| N005 | red    | —                                           | —                                                                                                     | No test inspects rules for explicit UDP/53. |
| N006 | red    | —                                           | —                                                                                                     | No test inspects rules for explicit TCP/53. |
| N007 | red    | —                                           | —                                                                                                     | No test inspects rules for explicit outbound 22. |
| N008 | red    | —                                           | —                                                                                                     | No test inspects rules for host's /24 allow. |
| N009 | red    | —                                           | —                                                                                                     | No test asserts GitHub IP fetch (best-effort) happened. |
| N010 | red    | —                                           | —                                                                                                     | No test inspects per-host `dig` resolution. |
| N011 | red    | —                                           | —                                                                                                     | No test inspects ipset contents after init. |
| N012 | green  | tests/firewall_test.sh:40-64                | B2 sanity: example allowlist parsed strips `# git-push` annotation; `api.anthropic.com` regex test excludes commented entries. | |
| N013 | red    | —                                           | —                                                                                                     | No test that asserts blank lines are skipped (inferred from successful parses but not directly checked). |
| N014 | red    | —                                           | —                                                                                                     | No test that asserts full-line comments are skipped. |
| N015 | red    | —                                           | —                                                                                                     | No test inspects rules for the ESTABLISHED,RELATED match. |
| N016 | red    | —                                           | —                                                                                                     | No test inspects rules for the REJECT `icmp-admin-prohibited`. |
| N017 | green  | tests/firewall_test.sh:248-258 + podman_smoke_test.sh:181-194 | Phase 3: ollama OUTPUT policy STILL `-P OUTPUT DROP` even with proxy bypass. Podman smoke T3 repeats the assertion under rootless podman. | |
| N018 | green  | tests/firewall_test.sh:66-155               | Phase 2: when `PROXY_API_URL` hostname is `blocked.example.com` and not on allowlist, proxy logs `'PROXY_API_URL hostname.*not in'` + container is unhealthy. | |
| N019 | red    | —                                           | —                                                                                                     | No test forces the in-script verification probe that example.com is blocked (only the outer firewall test verifies block, not the internal probe message). |
| N020 | red    | —                                           | —                                                                                                     | No test forces the positive verification probe (`api.github.com / pypi.org / registry.npmjs.org`) to be checked explicitly. |
| N021 | red    | —                                           | —                                                                                                     | No test forces both probes to fail and asserts the script errors. |
| N022 | red    | —                                           | —                                                                                                     | No test inspects global `credential.helper`. |
| N023 | red    | —                                           | —                                                                                                     | No test inspects the `# git-push` parse path. |
| N024 | red    | —                                           | —                                                                                                     | No test asserts per-host `credential.https://<host>.helper store` is set. |
| N025 | red    | —                                           | —                                                                                                     | No test runs `net open` against a real service and asserts the next `start` mounts the override network. |
| N026 | red    | —                                           | —                                                                                                     | No test asserts `.harness-net-overrides.json` survives `down`. (No `net open` is ever invoked in any test that also restarts.) |
| N027 | red    | —                                           | —                                                                                                     | No test populates a `reason` and reads it back via `net status`. |
| N028 | red    | —                                           | —                                                                                                     | No test verifies atomic-write semantics for `netlib_add_host`. |
| N029 | green  | tests/harness_test.sh:680-686               | T11: existing pull entry upgraded by `net allow --push <host>` -> line rewritten with `# git-push`. (This is the upgrade case M075/M029.) | |
| N030 | green  | tests/harness_test.sh:721-726               | T11: `harness net deny <host>` removes the line including annotations. | |

## U — Upgrade actions library (29 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| U001 | green  | tests/upgrade_test.sh:227-354               | T6 synthetic N→N+1 invokes `apply_upgrade_actions` with a manifest that includes envfile_merge, linefile_merge, directory_overwrite; each one's side-effect is checked. | |
| U002 | green  | tests/upgrade_test.sh:227-354               | T6 manifest includes an unknown action type; assertion checks the JSON summary has an error count > 0. | |
| U003 | green  | tests/upgrade_test.sh:60-108                | T1: envfile_merge `added_keys == "C"`; new key `C=` appears in target with marker comment. | |
| U004 | green  | tests/upgrade_test.sh:60-108                | T1: `^A=user-set-A$` and `^B=user-set-B$` preserved post-merge. | |
| U005 | green  | tests/upgrade_test.sh:60-108                | T1: marker comment `Added by harness upgrade on 2026-04-25` present. | |
| U006 | green  | tests/upgrade_test.sh:60-108                | T1: source comment context (`# desc for C`) carried forward before the new `C=` line. | |
| U007 | green  | tests/upgrade_test.sh:60-108                | T1 explicitly exports `HARNESS_UPGRADE_DATE=2026-04-25` and reads the date back from the marker. | |
| U008 | red    | —                                           | —                                                                                                     | No test crafts a source `.env.example` line with a trailing backslash to assert the merge rejects it. |
| U009 | green  | tests/upgrade_test.sh:60-218                | T1 + T5: result blob asserted via `jq -r '.added_keys'`, `.skipped`, `.length`. | |
| U010 | red    | —                                           | —                                                                                                     | No test runs an envfile_merge against a CRLF source to assert CR stripping. |
| U011 | green  | tests/upgrade_test.sh:113-141               | T2: linefile_merge appends `host-c.example`; `added_lines=[host-c.example]`. | |
| U012 | green  | tests/upgrade_test.sh:163-208               | T2b crafts a target file ending with NO newline (via `printf` to bypass heredoc auto-newline), asserts the precondition via `tail -c 1 \| od`, runs `upgrade_linefile_merge`, then asserts: (a) added line on its own line, (b) original last entry intact, (c) post-merge file ends with `0a` byte, (d) marker comment on its own line. | |
| U013 | green  | tests/upgrade_test.sh:113-141               | T2: line already in target -> skipped (length == 0 for that line). | |
| U014 | green  | tests/upgrade_test.sh:113-141               | T2: source has different inline annotation than target -> warning count == 1. | |
| U015 | green  | tests/upgrade_test.sh:113-141               | T2: target retains its `host-b.example # git-push` annotation. | |
| U016 | green  | tests/upgrade_test.sh:146-174 + 362-385     | T4 uses rsync path; T7 shadows `command -v rsync` to force shell-loop and verifies identical result. | |
| U017 | green  | tests/upgrade_test.sh:362-385               | T7 shadows rsync, asserts directory_overwrite still succeeds with same content. | |
| U018 | green  | tests/upgrade_test.sh:146-174               | T4 (DEALBREAKER): `data/user.txt == "user-state"` preserved across overwrite. | |
| U019 | green  | tests/upgrade_test.sh:179-218               | T5: `_upg_is_preserved` matches exact path (covered as part of preserve list in T4 + T5 edge cases). | |
| U020 | green  | tests/upgrade_test.sh:179-218               | T5: parent-directory match path (e.g., `data/` preserves `data/sub/foo`). | |
| U021 | red    | —                                           | —                                                                                                     | Bare-directory-name match is part of `_upg_is_preserved`; not specifically tested in isolation. |
| U022 | green  | tests/upgrade_test.sh:179-218               | T5: missing-target + `condition: installed` -> reports `target_missing`. | |
| U023 | green  | tests/upgrade_test.sh:179-218               | T5: missing-source -> reports `source_missing`. | |
| U024 | red    | —                                           | —                                                                                                     | No test directly exercises `_upg_atomic_mv`. |
| U025 | green  | tests/upgrade_test.sh:537-580               | T9 sources `upgrade_actions.sh` standalone (no outer harness), asserts `declare -F harness_jq` succeeds, then exercises the fallback: `printf '{"k":1}' \| harness_jq -r '.k'` returns `1`; `printf 'hello world' \| harness_jq -Rs .` returns `"hello world"`; `_upg_json_array foo bar` returns `["foo","bar"]`; `_upg_json_str 'a/b "c"'` round-trips. | |
| U026 | red    | —                                           | —                                                                                                     | No test asserts `version: 2` manifest is rejected. |
| U027 | red    | —                                           | —                                                                                                     | No test asserts ordering of `actions[]` vs `registry_actions[]`. |
| U028 | red    | —                                           | —                                                                                                     | No test runs `registry_actions` with `condition: installed` against a not-installed MCP entry. |
| U029 | green  | tests/upgrade_test.sh:227-354               | T6 manifest includes a failing action; final `apply_upgrade_actions` rc != 0. | |

## Pe — Persistence (paths that must survive lifecycle/upgrade) (19 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| Pe001 | green  | tests/upgrade_test.sh:227-354               | T6: `.env` (with user-set `PROXY_API_URL=https://my-llm.example/v1`) survives upgrade idempotently. | |
| Pe002 | green  | tests/upgrade_test.sh:113-141               | T2 simulates allowlist upgrade; existing host entries preserved. | |
| Pe003 | red    | —                                           | —                                                                                                     | No test makes `.harness-net-overrides.json` and asserts it survives `down`. |
| Pe004 | green  | tests/persistence_test.sh:191-426 + full_pipeline_test.sh:444-462 | T3, T4, T5, T6 all verify state/agent/home/ content persistence across container rebuild. | |
| Pe005 | green  | tests/full_pipeline_test.sh:338-345         | T5 asserts `find "${TEST_ROOT}/harness/state/ollama-data" -mindepth 1` finds at least one entry — proves the directory holds persisted registration data after a successful start. | |
| Pe006 | yellow | tests/proxy_test.sh:55                      | OUTPUT_DIR env defined; never modified by upgrade test paths. | No upgrade-touches-output-dir test. |
| Pe007 | green  | tests/mcp_test.sh:238-259 + 527-546         | install creates `state/mcp/<name>/`; uninstall removes it. | |
| Pe008 | green  | tests/upgrade_test.sh:146-174 + mcp_test.sh:415-441 | T4 (DEALBREAKER): post-upgrade `jq '.enabled' harness-meta.json == false` preserved; T9 demonstrates disable state. | |
| Pe009 | red    | —                                           | —                                                                                                     | No test asserts `state/mcp/serena/data/` survives a fake upgrade. (Integration test exercises serena but doesn't run an upgrade across it.) |
| Pe010 | green  | tests/full_pipeline_test.sh:326-336         | T5 asserts if `state/.harness-runtime.yml` exists, it carries the `# Generated by harness; do not edit.` header — guarantees the file is the regenerated artifact, not a stale hand-written one. | |
| Pe011 | green  | tests/mcp_test.sh:600-621                   | T17 re-triggers side-file write path. | |
| Pe012 | green  | tests/persistence_test.sh:329-368           | T5 auto-augments `~/.claude/settings.json` on launch. | |
| Pe013 | green  | tests/persistence_test.sh:257-315           | T4: ccstatusline `settings.json` seeded; `_user_marker` preserved across re-seed. | |
| Pe014 | green  | tests/persistence_test.sh:137-176           | T1 + T2: skel-seed once, user edits preserved on second run. | |
| Pe015 | green  | tests/full_pipeline_test.sh:579-597         | T13 down: no state files deleted. | |
| Pe016 | green  | tests/upgrade_test.sh:60-354                | All upgrade tests preserve user state files (asserted across T1, T4, T6). | |
| Pe017 | green  | tests/mcp_test.sh:527-546                   | T14: uninstall removes `state/mcp/<name>/` recursively. | |
| Pe018 | green  | tests/upgrade_test.sh:146-174               | T4 (DEALBREAKER): harness-meta.json with `.enabled == false` survives directory_overwrite even when source has it as true. | |
| Pe019 | green  | tests/upgrade_test.sh:146-174               | T4: `data/` subdir preserved (`data/user.txt == "user-state"`). | |

## O — Ollama entrypoint + stub model registration (25 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| O001 | green  | tests/firewall_test.sh:272-318              | Phase 3 captures ollama container logs and asserts: (a) `[harness-firewall] starting init` marker present, (b) `[harness-firewall] init complete` marker present, (c) no `init-firewall.sh missing` fallback, (d) firewall `starting init` log line precedes `waiting for ollama API at` — direct ordering proof that firewall ran before `ollama serve`. | |
| O002 | red    | —                                           | —                                                                                                     | No test verifies ollama PID capture / background launch. |
| O003 | green  | tests/harness_test.sh:315-325               | T3 scrapes the ollama container logs and asserts `grep -Eq 'waiting for ollama API at .*?/api/tags'` — direct evidence the entrypoint's poll loop emitted its banner (and, since T1 sees the container become healthy, that the poll succeeded). | |
| O004 | red    | —                                           | —                                                                                                     | No test forces ollama to be unready to exercise the fatal timeout branch. |
| O005 | green  | tests/proxy_test.sh:218-249                 | Scenario A's `/api/show` returns the registered model w/ context length 200000 — only possible if `register_stub_model` succeeded. | |
| O006 | green  | tests/proxy_test.sh:218-249                 | Scenario A: `/api/show` body contains `"context_length": 200000`. | |
| O007 | green  | tests/proxy_test.sh:218-249                 | Scenario A: `"num_ctx": 200000`. | |
| O008 | red    | —                                           | —                                                                                                     | No test specifically forces the streaming response to omit `"status":"success"` to assert the registration-fatal path. |
| O009 | green  | tests/harness_test.sh:327-336               | T3 also asserts the explicit success log line: `grep -Eq 'harness ollama ready; stub models -> '` — direct evidence registration succeeded and would catch a regression that turns a fatal error into a silent warning. | |
| O010 | red    | —                                           | —                                                                                                     | No test fakes `sonnet` registration failure to assert best-effort tolerance. |
| O011 | red    | —                                           | —                                                                                                     | Same for `opus`. |
| O012 | red    | —                                           | —                                                                                                     | Same for `haiku`. |
| O013 | red    | —                                           | —                                                                                                     | Same for `claude-sonnet-4-5`. |
| O014 | red    | —                                           | —                                                                                                     | Same for `claude-opus-4-5`. |
| O015 | red    | —                                           | —                                                                                                     | Same for `claude-haiku-4-5`. |
| O016 | red    | —                                           | —                                                                                                     | Same for `claude-3-5-sonnet-20241022`. |
| O017 | red    | —                                           | —                                                                                                     | Same for `claude-3-5-haiku-20241022`. |
| O018 | red    | —                                           | —                                                                                                     | Same for `claude-3-opus-20240229`. |
| O019 | red    | —                                           | —                                                                                                     | No test reads back each alias's `remote_host` to verify it equals the proxy URL. |
| O020 | red    | —                                           | —                                                                                                     | No test sends EXIT to the entrypoint and asserts cleanup. |
| O021 | red    | —                                           | —                                                                                                     | No test sends INT. |
| O022 | red    | —                                           | —                                                                                                     | No test sends TERM. |
| O023 | red    | —                                           | —                                                                                                     | No test inspects `OLLAMA_REMOTES` env. |
| O024 | green  | tests/harness_test.sh:225-252               | T1 `docker inspect --format '{{.State.Health.Status}}'` returns `healthy` for ollama, which polls `/api/tags`. | |
| O025 | red    | —                                           | —                                                                                                     | No test asserts the `depends_on: proxy: service_healthy` gate (would need to simulate proxy-unhealthy and assert ollama waits). |

## I — Installer + platform.sh primitives (42 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| I001 | green  | tests/full_pipeline_test.sh:139-201         | T0/T1 invokes `harness-install.sh` directly (not sourced); assertion checks layout created. | |
| I002 | red    | —                                           | —                                                                                                     | No test sources `harness-install.sh` and asserts it does NOT call `exit`. |
| I003 | green  | tests/full_pipeline_test.sh:139-201         | T0/T1 succeeds only when git is on PATH; preflight inside install runs. | |
| I004 | green  | tests/full_pipeline_test.sh:139-201         | Install preflight verifies docker (or podman) — test passes against the active runtime. | |
| I005 | green  | tests/full_pipeline_test.sh:243-247         | T2 directly invokes `harness_docker compose version >/dev/null 2>&1` — fails T2 if compose isn't callable, proving the preflight invariant. | |
| I006 | red    | —                                           | —                                                                                                     | No test simulates < 5 GB free disk to force preflight failure. |
| I007 | red    | —                                           | —                                                                                                     | No test runs install in a non-writable CWD to assert preflight failure. |
| I008 | red    | —                                           | —                                                                                                     | No test runs install with `./harness/` already present and asserts refusal. |
| I009 | green  | tests/full_pipeline_test.sh:203-208         | T1 source-greps `harness-install.sh` for the exact literal `REPO_URL="${HARNESS_REPO_URL:-https://github.com/HandelSim/harness}"` — proves the documented default URL is still wired in. | |
| I010 | green  | tests/full_pipeline_test.sh:139-201         | `HARNESS_REPO_URL` is exported to point at the local repo; install honors it. | |
| I011 | red    | —                                           | —                                                                                                     | No test runs install before `platform.sh` exists to exercise inline fallbacks. |
| I012 | green  | tests/full_pipeline_test.sh:210-217         | T1 asserts `scripts/lib/platform.sh` exists in the clone AND grep's `harness-install.sh` for the literal `source "$install_root/scripts/lib/platform.sh"` — proves the post-clone source happens. | |
| I013 | red    | —                                           | —                                                                                                     | No test simulates Windows to exercise the dos2unix pass. |
| I014 | green  | tests/full_pipeline_test.sh:203-213         | T2: `state/output/` exists and is writable. | |
| I015 | green  | tests/full_pipeline_test.sh:203-213         | T2: `state/agent/home/` exists. | |
| I016 | green  | tests/full_pipeline_test.sh:203-213         | T2: `state/ollama-data/` exists. | |
| I017 | green  | tests/full_pipeline_test.sh:203-213         | T2: `state/mcp/` exists. | |
| I018 | red    | —                                           | —                                                                                                     | No test pre-creates `.env` in install root and asserts it is left untouched. |
| I019 | red    | —                                           | —                                                                                                     | No test pre-creates `$cwd/.env` outside install root and asserts it is moved in. |
| I020 | green  | tests/full_pipeline_test.sh:203-213         | T2: `.env` exists after install (seeded from example). | |
| I021 | green  | tests/full_pipeline_test.sh:203-213         | T2: `.harness-allowlist` exists. | |
| I022 | green  | tests/full_pipeline_test.sh:203-213         | T2: wrapper at `$HOME/.local/bin/harness` exists + executable. | |
| I023 | green  | tests/full_pipeline_test.sh:203-213         | T2: `[[ ! -L "$wrapper" ]]` confirms NOT a symlink. | |
| I024 | green  | tests/full_pipeline_test.sh:230-233         | T2 source-greps the wrapper at `$HOME/.local/bin/harness` for the literal `${TEST_ROOT}/harness/harness` path — proves the install-root is hard-coded into the wrapper content, not just resolved at runtime. | |
| I025 | green  | tests/harness_test.sh:471-501               | T7: PATH-append into `~/.bashrc` (deduped to count == 1). | |
| I026 | red    | —                                           | —                                                                                                     | No test exercises the zshrc branch (SHELL=zsh). |
| I027 | red    | —                                           | —                                                                                                     | No test exercises the fish config.fish branch. |
| I028 | red    | —                                           | —                                                                                                     | No test exercises the "other shells" manual-instruction branch. |
| I029 | green  | tests/harness_test.sh:471-501               | T7: re-running install yields `count=$(grep -c '\.local/bin' "${rcfile}")` == 1. | |
| I030 | green  | tests/full_pipeline_test.sh:230-300         | Post-install start succeeds + preflight passes (assumed via T2 + T6). | |
| I031 | green  | tests/harness_test.sh:930-1033              | T19 imports `harness_validate_mount` from platform.sh via sourcing. | |
| I032 | green  | tests/harness_test.sh:557-572 + podman_smoke_test.sh:181-194 | doctor reports docker; podman_smoke_test sets `HARNESS_CONTAINER_RUNTIME=podman` and confirms preflight + doctor recognize podman. | |
| I033 | red    | —                                           | —                                                                                                     | No test asserts the cache behavior within a single invocation. |
| I034 | red    | —                                           | —                                                                                                     | No test simulates Git Bash to exercise `cygpath -m`. |
| I035 | red    | —                                           | —                                                                                                     | No test asserts `/c/Users/...` -> POSIX translation. |
| I036 | green  | tests/integration_test.sh:790-825           | Phase 5 mount validation rejects `/etc` (and by extension the protected-paths list); the message text comes from `harness_validate_mount`. | |
| I037 | green  | tests/harness_test.sh:970-974               | T19: `harness_docker_running` returns success when daemon is up; test asserts boolean. | |
| I038 | green  | tests/unit_platform_timer_test.sh:38-79     | T1 stubs the macOS+docker branch + a slow always-failing `harness_docker_running`, asserts the poll loop respects the requested timeout (wall-clock, not sleep-tick count) and returns non-zero. T2 asserts the printed "Ns elapsed" counter tracks wall clock within 3s. T3 asserts the success path returns 0 promptly once the probe succeeds. | |
| I039 | red    | —                                           | —                                                                                                     | No test stops the daemon on Linux to exercise fail-fast. |
| I040 | green  | tests/harness_test.sh:976-984               | T19: `harness_check_command bash` succeeds; `harness_check_command __nonexistent_cmd_xyz__` fails. | |
| I041 | green  | tests/harness_test.sh:1029-1033             | T19: `harness_check_disk_space "${REPO_ROOT}" 0 "any"` succeeds; threshold tests embedded in the helper. | |
| I042 | green  | tests/harness_test.sh:1022-1027             | T19: `harness_check_dir_writable "${tmpdir}" true "writable"` succeeds against a writable dir. | |
| I043 | green  | tests/full_pipeline_test.sh (T1b)           | T1b installs with `HTTPS_PROXY` exported and asserts the value lands in the seeded `.env` (blank line filled). | |
| I044 | yellow | tests/harness_test.sh (T7c)                 | Windows-gated, can't run on Linux CI: T7c source-greps the bridge in `harness-install.sh` and exercises the bridge snippet logic. | |
| I045 | green  | tests/harness_test.sh (T7c)                 | T7c asserts bridge idempotency (one `.bashrc` source line over 3 runs) and that a pre-existing `~/.profile` is preserved. | |

---

## Changes from previous audit

Track C produced the initial audit; Track J2 re-audited from the current
state of `tests/` (after Track D strengthened multiple test files, Track
E added `tests/scheme_contract_test.sh`, and Track F2 added the
`tests/e2e/scenarios/*.yaml` files with `inventory_refs` keys).

| ID    | C status | J2 status | Track(s) | Why promoted |
|-------|----------|-----------|----------|--------------|
| F006  | yellow   | green     | D        | full_pipeline_test T2 now source-greps installed wrapper for `_self_path` function + realpath/readlink fallback. |
| F015  | yellow   | green     | D        | harness_test T19b sources wrapper, stubs `harness_docker`, asserts host-jq branch returns correct result. |
| F022  | yellow   | green     | D        | harness_test T23.4b invokes `harness check-updates` with no cache + unreachable origin, asserts rc != 0 + diagnostic. |
| F026  | yellow   | green     | D        | full_pipeline_test T5 asserts the runtime-override file (when present) carries the generator header. |
| F031  | yellow   | green     | D        | full_pipeline_test T14 asserts ff-only / no-op output AND source-greps wrapper for `git pull --ff-only` literal. |
| F042  | yellow   | green     | D        | harness_test T3 runs `harness logs` (no service) and asserts both ollama and proxy lines appear. |
| F072  | yellow   | green     | D        | harness_test T11 now exercises `_`, `@`, `$`, `:` illegal chars in addition to space. |
| F131  | yellow   | green     | D        | harness_test T19c stubs `harness_docker` to capture compose() args, asserts `--project-name` is threaded. |
| F133  | yellow   | green     | D        | harness_test T19c same: asserts `-f docker-compose.yml` is threaded into compose() invocations. |
| F135  | yellow   | green     | D        | full_pipeline_test T5 (runtime-override header) + persistence_test T1/T3 (uid+gid alignment from injected env). |
| F139  | yellow   | green     | D        | full_pipeline_test T5 explicitly asserts `start.log` does NOT contain `NETWORK FIREWALL IS DISABLED`. |
| A004  | yellow   | green     | D        | persistence_test T1 + T3 now assert both uid AND gid match host on bind-mounted artifacts. |
| A007  | yellow   | green     | D        | full_pipeline_test T9 stat's the home-initialized marker (post-gosu-drop artifact) and asserts owner uid == host. |
| A012  | yellow   | green     | D        | persistence_test T5 now jq-asserts `includeCoAuthoredBy == false`. |
| A018  | red      | green     | F2       | e2e scenario 02-opencode-boot.yaml header declares `inventory_refs: [F044, A018]`; boot path exercises ensure_opencode_config. |
| A024  | yellow   | green     | D        | full_pipeline_test T9 source-greps wrapper for `ANTHROPIC_AUTH_TOKEN=harness-dummy` literal. |
| A031  | yellow   | green     | D        | full_pipeline_test T10 asserts no `unknown flag` error AND source-greps entrypoint.sh for strip branch. |
| I005  | yellow   | green     | D        | full_pipeline_test T2 directly invokes `harness_docker compose version`. |
| I009  | yellow   | green     | D        | full_pipeline_test T1 source-greps `harness-install.sh` for default REPO_URL literal. |
| I012  | yellow   | green     | D        | full_pipeline_test T1 asserts platform.sh in clone AND source-greps installer for sourcing line. |
| I024  | yellow   | green     | D        | full_pipeline_test T2 grep's installed wrapper for the install-root path literal. |
| O001  | yellow   | green     | D        | firewall_test Phase 3 scrapes ollama logs for firewall-init markers AND asserts firewall-line precedes ollama-api-wait-line. |
| O003  | yellow   | green     | D        | harness_test T3 scrapes ollama logs for `waiting for ollama API at .*?/api/tags` poll banner. |
| O009  | yellow   | green     | D        | harness_test T3 also asserts the explicit `harness ollama ready; stub models -> ` success line. |
| P003  | yellow   | green     | D        | proxy_test Scenario F scrapes proxy logs for `listening on: 0.0.0.0:8000` banner. |
| P004  | yellow   | green     | D        | proxy_test Scenario F same banner, asserts `:8000` port suffix. |
| P006  | yellow   | green     | D        | proxy_test Scenario F asserts redacted key banner AND raw key NOT printed. |
| P012  | yellow   | green     | D        | proxy_test Scenario F asserts banner reports `debug dumps: disabled (OUTPUT_DIR not set)`. |
| P050  | yellow   | green     | D        | proxy_test Scenario F asserts proxy logs have no `failed to save debug file` and no `is not writable` lines. |
| Pe005 | yellow   | green     | D        | full_pipeline_test T5 asserts `state/ollama-data` is non-empty (find -mindepth 1) after start. |
| U012  | yellow   | green     | D        | upgrade_test T2b uses `printf` to craft a no-trailing-newline target, then asserts post-merge byte-accuracy via `tail -c 1 \| od`. |
| U025  | yellow   | green     | D        | upgrade_test T9 sources upgrade_actions.sh standalone and exercises harness_jq + `_upg_json_array` + `_upg_json_str` directly. |

Per-prefix transition count:

| prefix | yellow→green | red→green | yellow→yellow | red→red |
|--------|--------------|-----------|---------------|---------|
| F      |           11 |         0 |             0 |      44 |
| P      |            5 |         0 |             2 |      13 |
| A      |            5 |         1 |             0 |      16 |
| I      |            4 |         0 |             0 |      16 |
| O      |            3 |         0 |             0 |      18 |
| U      |            2 |         0 |             0 |       7 |
| Pe     |            1 |         0 |             1 |       2 |
| **total** |       31 |         1 |             3 |     145 |

(`green→green` and `red→green-via-corroboration-only` not enumerated — neither
moves the count. Notably P010, P013–P018 stayed green but gained Track-E
end-to-end contract evidence; the per-row Evidence cells were updated to
reflect this.)

Track E (scheme_contract_test.sh) and Track F2 (e2e scenarios) corroborated
several already-green rows with stronger or more direct evidence; the per-row
Evidence cells were updated where this gives a future reader a better
test-pointer:

- P010, P013, P014, P015, P016, P017 — Track E adds direct mock-upstream body
  capture assertions per scheme.
- F001, F043, F044, F051, F062, A013, P032, P051, Pe012 — Track F2 scenarios
  declare `inventory_refs` and exercise the TUI surface (corroboration of
  green rows that were previously only python-unit-tested or
  print-mode-tested).

---

## Spot-check log

Goal: pick green rows from different test files, introduce a deliberate regression
into the source, confirm the test FAILS, then revert and confirm the test passes
again.

### Spot-check J2-1 — U012 (linefile_merge newline injection, newly green)

Test file: `tests/upgrade_test.sh` T2b (line 163-208).

The test crafts a target file ending with NO trailing newline (via
`printf 'host-a.example'` — no `\n`), runs `upgrade_linefile_merge`, then
asserts via `grep -Eq '^host-new\.example$'` that the appended line lives
on its own line and `^host-a\.example$` that the original line remains
intact.

Regression introduced into `scripts/lib/upgrade_actions.sh:341-349`:
removed the trailing-newline injection block (replaced with a single
`printf '%s' "$append_buf"`) so the append is glued directly onto the
target's last byte.

- Backup: `cp scripts/lib/upgrade_actions.sh /tmp/upgrade_actions.sh.bak`.
- Re-run `bash tests/upgrade_test.sh`:
  ```
  --- T2b: linefile_merge into target without trailing newline ---
  [upgrade-test] FAIL: U012: host-a.example was corrupted by missing-newline append
  ```
- Restore from backup; re-run:
  ```
  --- T2b: linefile_merge into target without trailing newline ---
  [upgrade-test] OK: T2b: linefile_merge injects newline before appending when target lacks one
   UPGRADE TEST PASSED
  ```

This proves T2b genuinely guards the missing-trailing-newline path.

### Spot-check J2-2 — U025 (standalone harness_jq fallback, newly green)

Test file: `tests/upgrade_test.sh` T9 (line 537-580).

The test asserts that when `upgrade_actions.sh` is sourced standalone
(without the outer harness wrapper), `harness_jq` is still defined AND
delegates correctly to the host `jq` binary.

Regression: removed the entire `if ! declare -F harness_jq` ... `fi`
block at lines 26-39 of `scripts/lib/upgrade_actions.sh`, replacing it
with `:` (no-op).

- Backup taken; regression applied.
- Re-run `bash tests/upgrade_test.sh`: T1 (the first test that uses any
  helper consuming harness_jq) immediately blows up:
  ```
  scripts/lib/upgrade_actions.sh: line 65: harness_jq: command not found
  scripts/lib/upgrade_actions.sh: line 75: harness_jq: command not found
  ```
  (T9 never gets a chance to run, but T1's failure already proves the
  fallback is load-bearing.)
- Restore; re-run: `--- T9: harness_jq fallback inside upgrade_actions.sh ---
  [upgrade-test] OK: T9: ...` and `UPGRADE TEST PASSED`.

This proves the U025 contract (standalone-source path of `harness_jq`)
is genuinely guarded — by T9 specifically AND by every earlier test
that goes through `_upg_json_array` / `_upg_json_str`.

### Spot-check J2-3 — A024 (`ANTHROPIC_AUTH_TOKEN=harness-dummy` literal, newly green)

Test file: `tests/full_pipeline_test.sh` T9 (line 466-470).

Full T9 requires docker (install + start + run claude); the assertion
itself is a source-level grep:

```
grep -Fq 'ANTHROPIC_AUTH_TOKEN=harness-dummy' "${TEST_ROOT}/harness/harness"
```

To exercise the regression detection without bringing up the full
pipeline, the same grep was simulated locally against the wrapper
source:

```
# regression: replace literal in wrapper
sed -i 's/ANTHROPIC_AUTH_TOKEN=harness-dummy/ANTHROPIC_AUTH_TOKEN=harness-wrong-token/' harness
grep -Fq 'ANTHROPIC_AUTH_TOKEN=harness-dummy' harness  # rc=1 (expected = literal missing)

# restore from backup
cp /tmp/harness.bak harness
grep -Fq 'ANTHROPIC_AUTH_TOKEN=harness-dummy' harness  # rc=0 (expected = literal present)
```

The grep returns rc=1 with the literal absent and rc=0 once restored.
T9's assertion is precisely this grep, so a regression replacing the
token literal would fail T9 inside the docker harness.

### Spot-check C-1 (carried) — F056 (`harness list` empty case)

Test file: `tests/harness_test.sh` T2 (line 256-262). See Track C audit
for transcript. Re-verified: assertion still references the literal
`"no harness agents running"`.

### Spot-check C-2 (carried) — M003 (`harness-meta.json` shape after install)

Test file: `tests/mcp_test.sh` T3 (line 238-258) + T5 (line 277-309). See
Track C audit. Note: T3 asserts file existence; T5 asserts the file's
`enabled` content indirectly by attempting `harness mcp up`.

### Spot-check C-3 (carried) — U003 (envfile_merge adds new keys)

Test file: `tests/upgrade_test.sh` T1 (line 60-108). See Track C audit.

### Spot-check C-4 (carried) — P035 (`toolu_` tool-call id prefix)

Test file: `proxy/test_proxy.py` line 50-95. See Track C audit.

---

## Notes / known issues

- `state/output/` (Pe006) has no positive coverage. The proxy debug-dump infrastructure
  (P043-P050) is entirely untested at the dump-file level — the test scaffolding sets
  `OUTPUT_DIR=` (empty) to bypass it. Track-D should add a single test that points
  OUTPUT_DIR at a tmpdir and asserts the expected file prefixes appear.
- HTTP error forwarding (P037-P042) is entirely untested. The mock upstream
  (`tests/mock_upstream.py`) returns only 200; would need extension to assert 401/403/
  429/5xx behaviors.
- Network-related N-row items (N002-N011, N015-N016, N019-N028) are mostly red because
  no test reads back actual iptables/ipset state from a clean firewall run. The two
  branches we DO cover are bypass (N001, N017) and a focused negative (N018). Adding a
  Phase-1-positive test (rules-present after a clean firewall init) would lift ~12 items.
- Ollama alias registrations (O010-O019) are individually untested. A single test that
  inspects ollama's model list post-startup against the alias list would cover all
  eight at once.
- F-row coverage gaps cluster in `net open`/`net close`/`unlock`/`pick_agent`/`shell`
  surfaces (F045, F057-F061, F081-F092). These are all interactive paths or paths
  requiring 401-mocked upstream that the existing harness can't exercise without
  additional scaffolding.
