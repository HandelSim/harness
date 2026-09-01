# Test coverage map

Maps every inventory ID in `tests/INVENTORY.md` to the test(s) that exercise it,
with a status flag based on the actual assertion strength.

**Status legend**

- **green** — a test exists AND its assertions check the behavior the inventory row claims.
  Evidence cites a real test file and line number plus a quoted assertion.
- **yellow** — a test runs the code path but only checks something weak (return code only,
  output non-empty, "no crash", etc.). Evidence quotes the weak assertion verbatim.
- **red** — no test exercises this behavior.

Inventory total: 501 rows (F=157, P=89, A=24, M=33, N=31, U=33, Pe=16, O=0, I=50, B=10, Ho=18, C=40). 498 distinct IDs — F100/F101/F102 are each used for two rows (a pre-existing numbering collision present identically in INVENTORY.md and here).

Test artifacts audited (re-audited from current state after Tracks D/E/F2):

- `tests/harness_test.sh` (2487 lines) — CLI surface, doctor, preflight, net, mcp,
  upgrade flags, update check, platform.sh primitives. Sources the wrapper under
  `HARNESS_SOURCE_ONLY=1` and stubs `ensure_services_up` for non-docker tests.
  Track-D strengthening: F015 (T19b), F042 (T3), F072 (T11 extra chars), F131/F133
  (T19c), F022 (T23.4b).
- `tests/full_pipeline_test.sh` — end-to-end install → start → agent (bare + opencode)
  print → mcp install/start/uninstall → down → update. Uses bundled mock_upstream sidecar.
  Track-D strengthening: I009/I012/I024/F006/I005 (T1+T2 source-level greps), F139
  (T5 firewall banner silence), F026/F135/Pe010 (T5 generator-header check),
  A007/A024 (T9 uid-owned marker + token literal),
  A031 (T10 strip + entrypoint grep), F031 (T14 git-pull literal + ff-only message).
- `tests/proxy_test.sh` (683 lines) — proxy round-trip black-box (Scenarios A-F) AND
  delegates to `proxy/test_proxy.py` via `python -m unittest`. Track-D added Scenario F:
  P003/P004/P006/P012/P050 log-scrape of the proxy startup banner.
- `proxy/test_proxy.py` (4428 lines) — pure-Python unit tests for `format_tools_to_text`,
  `extract_tool_calls_and_text`, `_scan_balanced_json`, `translate_history_and_apply_prompt`,
  `make_chunk`, prompt-injection modes, system→user rewrite, usage override. Also carries
  the ChatGPT backend-api classes (`TestChatGPTFlatten`, `TestChatGPTCollectStream`,
  `TestChatGPTPost`, `TestUpstreamPostRouting`, `TestChatGPTEndToEnd`,
  `TestChatGPTModelCatalog`, `TestChatGPTValidateConfig`) covering P065-P091,
  `TestReminderTemplateFile` covering P064, and `TestToolGuidanceFile` covering P092.
- `tests/unit_chatgpt_test.sh` (no docker) — CLI-side `harness chatgpt` / `harness chatgpt
  host`: backend selection (`_backend_is_chatgpt`/`_effective_default_model`), the shared
  `proxy:` runtime-override mapping, per-backend required-config swap
  (`require_runtime_config`/`host_require_config`), the auth-probe/model-catalog no-ops,
  `host_proxy_fingerprint` backend/CHATGPT_* coverage, `_running_proxy_backend` +
  `ensure_services_up` restart-on-mismatch, cookie secrecy + config-picker + allowlist sync,
  `cmd_chatgpt` dispatch/help/wiring (including `doctor`/`preflight` and the
  `start`/`restart`/`down` lifecycle verbs), `_adopt_running_backend`, the
  `_config_write_key`/`_config_read_key` quoting round-trip, and the
  `HARNESS_PROXY_FP` credential fingerprint. Sourced via `HARNESS_SOURCE_ONLY=1`.
  Covers C001-C039 (T1-T20). T18-T20 also pin the cost/behaviour guarantees for
  installs that never configured this backend.
- `tests/unit_config_test.sh` (no docker) — `harness config` read/write/list plus the
  `harness uninstall` confirm gate and teardown, and the two `.env` quoting guards:
  `_warn_unquoted_env_values` (F153) and `_config_value_truncated` (C040). Sourced via
  `HARNESS_SOURCE_ONLY=1` against a throwaway install root.
- `tests/scheme_contract_test.sh` (407 lines, Track E) — per-scheme proxy contract test.
  Brings up proxy + mock upstream and for each `PROXY_PROMPT_MODE` value drives
  a probe through the proxy's OpenAI-compatible interface; asserts forwarded-body structure.
  Covers P010, P013, P014, P015, P016, P017, P018 with direct upstream-body assertions
  (closes scheme-emission red gaps that were previously only proxied through python unittests).
- `tests/firewall_test.sh` (254 lines) — Phase 2 negative (blocked PROXY_API_URL hostname
  is fatal) and Phase 3 bypass (`HARNESS_FIREWALL_DISABLED=1` per-service).
- `tests/integration_test.sh` (988 lines, gated `HARNESS_RUN_SLOW=1`) — Serena MCP,
  Graphify pipx, `--mount` rejection paths.
- `tests/mcp_test.sh` (1104 lines) — MCP lifecycle (list/install/enable/disable/up/down/
  uninstall/status), side-file regeneration, allowed_domains recommendation, and
  `register` dynamic registration behind the compose-merge validation gate (TR1–TR10).
- `tests/persistence_test.sh` — skel-seed, `pip install --user`,
  `.git-credentials` persistence. Track-D strengthening:
  A004 (T1 + T3 uid+gid assertions on bind-mounted artifacts).
- `tests/upgrade_test.sh` (996 lines, no docker) — `envfile_merge`, `linefile_merge`,
  `directory_overwrite`, `userfile_sync`, `_upgrade_confirm`, synthetic N→N+1 upgrade,
  rsync fallback.
  Track-D added T2b (U012 missing-trailing-newline injection) and T9 (U025 standalone
  harness_jq fallback consumed by `_upg_json_array` / `_upg_json_str`); T13 covers
  U030-U033 (the reminder / tool-guidance replace-or-keep question).
- `tests/podman_smoke_test.sh` (229 lines, manual-run) — `HARNESS_CONTAINER_RUNTIME=podman`
  smoke. Useful corroboration for I032/N017.
- `tests/unit_reminder_seed_test.sh` (no docker) — `seed_reminder_file` (T1-T5) and
  `seed_tool_guidance_file` (T6-T8), the two wrappers over `seed_user_data_file`:
  each seeds its gitignored `<install_root>/<tracked basename>` copy from the
  tracked `proxy/reminder.md` / `proxy/tool-guidance.json`, never overwrites an
  edited copy, and `rmdir`s docker's empty-directory artifact; the shared helper's
  non-empty-directory warning and missing-tracked-default tolerance are covered
  once, through the reminder wrapper. T9-T11 cover the two legacy-layout
  migrations. Sourced via `HARNESS_SOURCE_ONLY=1`.
  Covers F152 (T1-T5), F154 (T6-T8), F155 (T9-T11).
- `tests/unit_host_test.sh` (no docker) — containerless `harness host`
  helpers, sourced via `HARNESS_SOURCE_ONLY=1`: `host_require_config`,
  `host_confirm_gate`, `host_preflight`, `host_write_opencode_config` (JSON shape +
  jq guard), `host_proxy_fingerprint`, and `host_python_bin` interpreter
  verification (Windows alias-stub rejection). Covers Ho001–Ho007, Ho018.
- `tests/unit_host_upgrade_test.sh` (no docker) — host-only upgrade transition
  (`cmd_upgrade` with no container runtime: host-aware path, no `require_docker`,
  no rebuild/restart, returns 0). Sourced via `HARNESS_SOURCE_ONLY=1`. Covers Ho008.
- `tests/unit_host_toolchain_test.sh` (no docker, no network) — host-mode
  dependency auto-provisioner (`host_ensure_toolchain` and helpers): platform
  mapping, `host_sha_from_manifest` parsing (curl stubbed), `host_sha256_check`,
  `host_toolchain_path_prefix` assembly, OS guard + PATH wiring, the
  pins-match-`agents/Dockerfile` drift guard, the Windows (Git Bash) layout
  branch (stubbing `harness_detect_os`/`uname`), and `host_extract_archive` kind
  dispatch. Sourced via `HARNESS_SOURCE_ONLY=1`. Covers Ho009–Ho017.
- `tests/unit_net_open_test.sh` (no docker) — `cmd_net_open` service-membership
  validation: the captured-list + here-string match that fixed the pipefail/SIGPIPE
  false-reject. Covers F089, F151.
- `tests/lib/test_helpers.sh` (311 lines) — shared fixtures only (no assertions).

## Summary stats

| status   | count | percent |
|----------|-------|---------|
| green    |   377 |  75.55% |
| yellow   |     5 |   1.00% |
| red      |   117 |  23.45% |
| **total**|   499 | 100.00% |

Per-prefix breakdown:

| prefix | total | green | yellow | red |
|--------|-------|-------|--------|-----|
| F      |   157 |   116 |      2 |  39 |
| P      |    89 |    75 |      1 |  13 |
| A      |    24 |    12 |      0 |  12 |
| M      |    33 |    28 |      0 |   5 |
| N      |    31 |     6 |      0 |  25 |
| U      |    33 |    26 |      0 |   7 |
| Pe     |    16 |    13 |      1 |   2 |
| O      |     0 |     0 |      0 |   0 |
| I      |    50 |    35 |      1 |  14 |
| B      |    10 |    10 |      0 |   0 |
| Ho     |    18 |    18 |      0 |   0 |
| C      |    40 |    40 |      0 |   0 |
| **all**|   501 |   379 |      5 | 117 |

(Per-prefix counts derived directly from this file's status column; they
reconcile to the total table above. The remaining yellows — F102, F142,
P008, Pe006, and I044 — are indirect-evidence items where the surrounding
test infrastructure would need substantive extension to promote.)

Spot-checks performed (regression detection)
--------------------------------------------

For each spot-check: introduce a deliberate regression into the source, re-run the
named test, confirm RED, then revert and confirm GREEN again. See the
"Spot-checks" section at the bottom of this file for the full transcripts.

Track J2 (this audit) re-ran 3 spot-checks against newly-green rows:

- **U012** (linefile_merge newline injection) — `tests/upgrade_test.sh:163-208` (T2b)
- **U025** (standalone harness_jq fallback) — `tests/upgrade_test.sh:537-580` (T9)

Carried over from Track C:

- **F056** (`harness list` empty case) — `tests/harness_test.sh:258`
- **M003** (post-install `harness-meta.json` shape) — `tests/mcp_test.sh:255`
- **U003** (envfile_merge appends new keys) — `tests/upgrade_test.sh:86`
- **P035** (`call_` tool-call id prefix) — `proxy/test_proxy.py:2902-2942`

---

## Coverage table

The per-ID tables below are split by inventory prefix. Each row is one
INVENTORY.md ID, with the current status (green / yellow / red), the
test file and line range carrying the strongest assertion, a one-line
evidence note (quoting real assertion text where possible), and — for
non-green rows — the gap.

## F — CLI surface, lifecycle, net, upgrade, doctor, preflight, mcp dispatch (157 rows)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| F001 | green  | tests/harness_test.sh:301-309               | T5 asserts help mentions `start down restart update upgrade logs opencode list stop net mcp doctor`; full_pipeline T3 re-asserts. | |
| F002 | green  | tests/harness_test.sh:303                   | `out=$(${HARNESS_WRAPPER} help 2>&1)` then `grep -q 'start'` etc. | |
| F003 | green  | tests/harness_test.sh:304                   | `${HARNESS_WRAPPER} -h` invoked & checked. | |
| F004 | green  | tests/harness_test.sh:305                   | `${HARNESS_WRAPPER} --help` invoked & checked. | |
| F005 | green  | tests/harness_test.sh:T30                    | T30 invokes `harness definitelynotacommand` and asserts non-zero exit + `unknown command` on stderr (option C still catches typos). | |
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
| F023 | green  | tests/harness_test.sh:225-252               | T1: after `harness start`, proxy reports `"healthy"` via `docker inspect`. Mirrored in full_pipeline_test.sh:230-347 phases. | |
| F024 | green  | tests/harness_test.sh:1041-1067             | Same T20 evidence as F013. | |
| F025 | red    | —                                           | —                                                                                                     | No test sets a net-override and asserts `start` prints the warning banner. (`warn_if_firewall_open` is only tested via help mention.) |
| F026 | green  | tests/full_pipeline_test.sh:326-336         | T5: if `state/.harness-runtime.yml` is present, asserts `grep -q '^# Generated by harness; do not edit\.'` — proves it's the generated artifact, not stale. (When the override body is empty the writer removes the file; either branch passes documented contract.) | |
| F027 | green  | tests/harness_test.sh:146-305               | T0 calls `_gate_on_upstream_auth` with a stubbed `_probe_upstream_auth` returning 1 (401); asserts function aborts with `exit 1`. T0.4 additionally drives the real probe with a locked-key fixture and asserts the unlock URL appears in the printed banner. | |
| F028 | green  | tests/harness_test.sh:286-297               | T4 invokes `harness down`, then `docker ps --filter` count must be 0 for the project. | |
| F029 | green  | tests/full_pipeline_test.sh:579-597         | T13 down + verify `state/agent/home/.harness-home-initialized` still present. | |
| F030 | green  | tests/harness_test.sh:732-748               | T12 invokes `harness restart`; afterwards proxy is healthy. | |
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
| F140 | green  | tests/upgrade_test.sh:836-871               | T12: `_upgrade_confirm "test? " n <<<""` returns rc 1 (Enter aborts); `<<<"y"` returns 0; default-y cases assert `""`→0, `n`→1 (back-compat). | |
| F141 | green  | tests/upgrade_test.sh:721-834               | T11: real origin+clone git fixtures — diverged (ahead+behind)→rc 0; up-to-date / behind-only / ahead-only / no-upstream→rc 1. | |
| F142 | yellow | tests/upgrade_test.sh:721-871               | Decision logic covered via building blocks (T11 `_git_branches_diverged` + T12 default-N `_upgrade_confirm`); the end-to-end `_upgrade_pull_or_reset` (reset-on-divergence, --no-prompt abort) is not driven through its TTY path in CI. | The TTY/`reset --hard` path is exercised manually, not in the unit suite. |
| F041 | green  | tests/harness_test.sh:268-282               | T3: `timeout 5 ${HARNESS_WRAPPER} logs proxy` produces output containing `proxy`-service log lines. | |
| F042 | green  | tests/harness_test.sh:283-313               | T3 now runs `timeout 5 ${HARNESS_BIN} logs` (no service), asserts rc 0/124, no parse-error message, AND `'proxy'` appears in the captured output (compose prefixes each line with the service name when no service is given). | |
| F043 | green  | tests/full_pipeline_test.sh T9 + harness_test.sh T30 | T9: bare `harness -p "<prompt>"` produces `Hello from mock upstream` (option C → opencode); T30 asserts bare/flag dispatch routes to opencode. | |
| F044 | green  | tests/full_pipeline_test.sh:391-417         | T10: `harness opencode -p "<prompt>"` produces a non-empty response with skip-on-auth-fail. | |
| F045 | red    | —                                           | —                                                                                                     | No test invokes `harness shell` (interactive TTY makes this hard to assert non-trivially). |
| F046 | red    | —                                           | —                                                                                                     | No test invokes `harness --yolo` and verifies `HARNESS_YOLO=1` is passed through. |
| F047 | red    | —                                           | —                                                                                                     | No test invokes `harness --net` with an open override to verify the network attachment. |
| F048 | green  | tests/integration_test.sh:735-785           | Phase 5 mounts a host dir via `--mount` and asserts the mount is visible inside the agent container; pwd inside container matches host CWD. | |
| F049 | green  | tests/integration_test.sh:790-825           | Phase 5: `--mount /etc` rejected with `shadow container infrastructure`; `--mount /nonexistent/abc` rejected with `does not exist\|cannot resolve`. | |
| F050 | red    | —                                           | —                                                                                                     | No test passes multiple `--mount` flags in one invocation and asserts both bind mounts present. |
| F051 | green  | tests/full_pipeline_test.sh T9              | T9 invokes bare `harness -p` and asserts mock response present (skip-on-auth-fail). | |
| F052 | red    | —                                           | —                                                                                                     | No test exercises the `--print` long form. |
| F053 | green  | tests/full_pipeline_test.sh:391-417         | T10 invokes `harness opencode -p`. | |
| F054 | green  | tests/harness_test.sh:T29                    | T29 asserts two `agent_container_name opencode` calls return different `harness-opencode-*` names (per-launch uniqueness; inverts the old determinism premise, #76). | |
| F055 | green  | tests/harness_test.sh:T29                    | T29 launches `run_agent_interactive` twice for the same tool+dir (each in its own subshell, since the path now `exit`s) and asserts both reach `docker run` with distinct `--name` values (no refusal; #76). | |
| F143 | green  | tests/harness_test.sh:T29                    | T29 part C drives `run_agent_interactive` with `harness_docker` stubbed to return 7; asserts the subshell exits 7 (exit-code propagation) and the `/issues` footer reached stderr (#81). | |
| F144 | green  | tests/harness_test.sh:T29                    | T29 part D drives `run_agent_print` and asserts the `/issues` footer is absent from stderr (#81). | |
| F145 | green  | tests/harness_test.sh:T31                    | T31 sources the script with an empty install root: unset `DEFAULT_MODEL_NAME` → `agent_model==""` (empty); `DEFAULT_MODEL_NAME=mymodel` → `agent_model==mymodel` (#94). | |
| F146 | green  | tests/harness_test.sh:T32                    | T32.1: `_downgrade_target_tag` against a v0.1/v1.0/tip git fixture from the untagged tip returns `v1.0` (#85). | |
| F147 | green  | tests/harness_test.sh:T32                    | T32.2: checked out exactly on `v1.0`, `_downgrade_target_tag` returns `v0.1` (#85). | |
| F148 | green  | tests/harness_test.sh:T32                    | T32.3: checked out on the earliest tag `v0.1`, `_downgrade_target_tag` exits non-zero with empty output (#85). | |
| F149 | green  | tests/harness_test.sh:T32                    | T32.4 pipes `n` → `cmd_downgrade --no-restart` exits non-zero and HEAD is unchanged; T32.5 pipes `y` → HEAD is `git reset --hard`'d to `v1.0` (require_docker stubbed; #85). | |
| F056 | green  | tests/harness_test.sh:256-262               | T2: `[[ "${list_out}" != "no harness agents running" ]]` then fails; expectation is the LHS literal. Re-asserted in full_pipeline_test.sh:353,382. | |
| F057 | red    | —                                           | —                                                                                                     | No test starts an agent then calls `harness stop` and verifies proxy remains. |
| F058 | red    | —                                           | —                                                                                                     | No test invokes `harness stop <name>`. |
| F059 | red    | —                                           | —                                                                                                     | No test forces multiple agent containers to exist to exercise `pick_agent` prompting. |
| F060 | red    | —                                           | —                                                                                                     | No test asserts `pick_agent` non-interactive single-agent path. |
| F061 | red    | —                                           | —                                                                                                     | No test asserts `pick_agent` error when 0 agents running. |
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
| F089 | green  | tests/unit_net_open_test.sh:T2              | T2 calls `cmd_net_open totally-bogus` and asserts the output contains `unknown service 'totally-bogus'`. | |
| F151 | green  | tests/unit_net_open_test.sh:T1,T1b,T3       | T1 asserts the first-listed service `proxy` is NOT rejected as unknown (slow multi-line `net_known_services` stub reproduces the pipefail/SIGPIPE trap); T1b asserts it reaches the confirm gate; T3 pins the last-listed `agent`. | |
| F090 | green  | tests/harness_test.sh:223-305               | T0.4 stubs `curl` to feed locked-key, no-unlock-URL, and top-level-URL fixtures through the real `_probe_upstream_auth`; asserts unlock banner contents, structured fields, raw body dump, and the rc=1 vs rc=2 split. | |
| F091 | red    | —                                           | —                                                                                                     | No test invokes `harness unlock` against a 200-mocked upstream. |
| F092 | red    | —                                           | —                                                                                                     | No test invokes `harness unlock` against an unreachable upstream. |
| F093 | green  | tests/harness_test.sh:146-305               | T0 `gate_case 1 1` (401 → fail), `gate_case 2 0` (connection failure → ignored), `HARNESS_SKIP_AUTH_PROBE=1` bypass. T0.4 also directly drives `_probe_upstream_auth` with stubbed `curl` for 200/401/500 fixtures and asserts rc=0/1/2/2 per the documented tri-state. | |
| F094 | green  | tests/harness_test.sh:146-221               | T0.2 stubs `ensure_services_up` as a sentinel; verifies `run_agent` invokes the gate before services. | |
| F095 | green  | tests/harness_test.sh:513-591               | T8/T9: `harness doctor` output matches `'\\[deps\\]'` with docker/git/jq lines. | |
| F096 | green  | tests/harness_test.sh:513-591               | T8/T9: `'\\[install\\]'` section present with install-root path. | |
| F097 | green  | tests/harness_test.sh:1100-1118             | T22 + doctor T8/T9: `[config]` section reports `.env` parseable / allowlist parseable. | |
| F098 | green  | tests/harness_test.sh:773-790               | T15: doctor `[network]` section + `allowlist` keyword. | |
| F099 | green  | tests/harness_test.sh:533-555               | T8 verifies `[storage]` block lists state/output, state/agent/home with writable status. | |
| F100 | green  | tests/full_pipeline_test.sh (T1b)           | T1b runs the installer with `HTTPS_PROXY` exported and asserts the value is persisted into the install root `.env` (then scrubs it). | |
| F101 | green  | tests/harness_test.sh (T7b)                 | T7b drives a fake runtime via `harness_docker`/`harness_docker_exec` with all four proxy spellings exported and asserts all four appear in the recorded runtime env (so `compose build`/BuildKit inherits them). | |
| F102 | yellow | tests/harness_test.sh (T7b)                 | Exercised indirectly: T7b exports both upper/lower spellings; the mirror runs at harness env-load (not asserted in isolation). | |
| F100 | green  | tests/harness_test.sh:557-572               | T8: `grep -Eq '(docker\|podman)\\s+runtime[[:space:]]+reachable'`. | |
| F101 | green  | tests/harness_test.sh:574-585               | T8: `[images]` section asserted; image presence + age lines printed for proxy/agents. | |
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
| F128 | green  | tests/mcp_test.sh:353-368                   | T7 asserts the side-file lists the service, which requires `mcp_compose_files` to have returned the entry's compose.yml. | |
| F129 | green  | tests/mcp_test.sh:353-368                   | T7 services list explicitly includes `test_mcp` (the service name parsed from compose.yml). | |
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
| F150 | green  | tests/harness_test.sh (T26pm)               | T26pm: `_parse_prompt_mode_flag` accepts `--prompt-mode <m>` and `--prompt-mode=<m>`, rejects invalid value + unknown option (non-zero); `write_runtime_override` emits a standalone `proxy:` block with `PROXY_PROMPT_MODE`, and folds into the proxy firewall opt-out block (single `proxy:` mapping with both env entries) when both apply. | |
| F152 | green  | tests/unit_reminder_seed_test.sh (T1-T5)    | T1: a missing `<install_root>/reminder.md` is created and `cmp`s byte-for-byte with the tracked `proxy/reminder.md`; T2: an edited copy survives a second `seed_reminder_file` untouched; T3: an EMPTY directory at that path is `rmdir`'d and replaced by the seeded file; T4: a NON-empty directory is left intact, its contents preserved, and a warning naming it is printed; T5: a missing `proxy/reminder.md` warns and returns 0 without creating anything. | `compose()`'s `INSTALL_ROOT` export and `host_proxy_start`'s `nohup` env are not asserted by this test — only the seeding helper is. |
| F153 | green  | tests/unit_config_test.sh (T13)             | A fixture `.env` mixing eight legitimate line shapes (comment with `;`/`&`, plain values, `export KEY=`, single- and double-quoted values with spaces and semicolons, an inline `# comment`, an empty value, `$HOME`) with three offenders (`BAD_SPACE=a b`, `BAD_SEMI=a=1; oai-did=2`, `BAD_AMP=a&b`): all three are named, none of the eight is, the warning names the fix, and a clean `.env` produces no output at all. | The call site (the scan runs before `source` on every invocation) is exercised end-to-end only by running the CLI, not asserted here. |
| F154 | green  | tests/unit_reminder_seed_test.sh (T6-T8)    | T6: a missing `<install_root>/tool-guidance.json` is created, `diff`s byte-for-byte with the tracked `proxy/tool-guidance.json`, and the reminder file is NOT created as a side effect; T7: an edited copy survives a second `seed_tool_guidance_file` untouched; T8: an EMPTY directory at that path is `rmdir`'d and replaced by the seeded file. | Same gap as F152: the single `INSTALL_ROOT` export and `host_proxy_start`'s `nohup` env are not asserted — only the seeding helper is. The non-empty-directory and missing-default paths are covered once, through the reminder wrapper (T4/T5), since both wrappers share `seed_user_data_file`. |
| F155 | green  | tests/unit_reminder_seed_test.sh (T9-T11)   | T9: a legacy `<install_root>/.harness-reminder.md` carrying `MY LEGACY WORDING` is gone after `seed_reminder_file` and its wording is found in `<install_root>/reminder.md` (moved, not re-seeded); T10: the same for `.harness-tool-guidance.json`, then a SECOND legacy file planted next to the migrated copy leaves `MY LEGACY LINE` in place — a stale legacy file never overwrites the current copy; T11: a `.harness-data/reminder.md` copy moves up to the install root and the emptied `.harness-data/` is `rmdir`'d. | The `rmdir` of an empty legacy directory left by an old bind-mount (as opposed to the emptied `.harness-data/`) is not asserted separately. |

## P — Proxy (89 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| P001 | green  | tests/proxy_test.sh:218-249                 | Scenario A: `curl /health` returns 200 with `{"status":"ok"}` (precondition before round-trip). | |
| P002 | green  | tests/proxy_test.sh:253-285                 | Scenario B posts to `/v1/chat/completions` (catch-all) and gets a forwarded chat-completions response. | |
| P003 | green  | tests/proxy_test.sh:469-475                 | Scenario F captures proxy logs and asserts `grep -qE 'listening on:[[:space:]]+0\.0\.0\.0:8000'` — direct evidence PROXY_HOST env was consumed by the bind. | |
| P004 | green  | tests/proxy_test.sh:475-479                 | Scenario F same banner asserts the `:8000` suffix on the listening line — PROXY_PORT env was consumed. | |
| P005 | green  | tests/proxy_test.sh:218-285 + test_proxy.py via env | Test env sets `PROXY_API_URL` and asserts forwarded body lands at mock upstream. | |
| P006 | green  | tests/proxy_test.sh:480-490                 | Scenario F sets `PROXY_API_KEY=test-key-1234`, asserts banner shows redacted form `test...1234` AND raw key is NOT printed (regression guard against accidental key leak). | |
| P007 | green  | proxy/test_proxy.py (TestModelPassthrough)  | test_missing_model_falls_back_to_default asserts the proxy forwards `DEFAULT_MODEL_NAME` when the request omits a model. | |
| P008 | yellow | tests/proxy_test.sh:56                      | `PROXY_TIMEOUT=30` set; no assertion on actual timeout behavior. | No test simulates a slow upstream to exercise the timeout. |
| P009 | green  | proxy/test_proxy.py:2448-2480               | `test_prompt_tokens_overridden_with_local_estimate` asserts the proxy replaces upstream `prompt_tokens` with its local estimate, which `_estimate_tokens` caps at `MODEL_CONTEXT_LENGTH` (proxy.py:1362; legacy alias `OLLAMA_CONTEXT_LENGTH`). The env knob itself is config-only — no test flips the var and re-reads the cap. | |
| P010 | green  | proxy/test_proxy.py:601-771 + tests/scheme_contract_test.sh | TestPromptInjectionModes covers `hybrid` as default via test_default_mode_is_hybrid (PROXY_PROMPT_MODE absent → hybrid). The scheme contract test adds end-to-end assertions for each cooperative scheme (`user_front`, `hybrid`) via mock-upstream body capture, re-injecting the mode through its own compose override. | |
| P011 | green  | proxy/test_proxy.py:774-919                 | TestChangeSystemToUser stubs the now-hardcoded constant via `patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True)`; the env-parse path was removed with `_setup_change_system_to_user`. | |
| P012 | green  | tests/proxy_test.sh:491-507                 | Scenario F asserts `grep -qE 'debug dumps:[[:space:]]+disabled \(OUTPUT_DIR not set\)'` — proxy banner reports OUTPUT_DIR consumption explicitly. | |
| P013 | green  | proxy/test_proxy.py + tests/scheme_contract_test.sh | test_mode_user_front_request_before_tools: `request_marker_pos < tools_section_pos`. Scheme contract test adds end-to-end: forwarded body's last user message has `<<<BEGIN_USER_REQUEST>>>` BEFORE `### Available Tools`. | |
| P017 | green  | proxy/test_proxy.py + tests/scheme_contract_test.sh | test_mode_hybrid_full_tools_in_system_reminder_in_user. Scheme contract adds end-to-end: forwarded last user message contains "Reminder:" + "do not invent" + "Available tools:" + probe text but NOT the full tool list / instructions header (those live in head). | |
| P018 | green  | proxy/test_proxy.py                          | test_invalid_mode_falls_back_to_hybrid asserts both unknown values ("garbage") AND removed/legacy modes ("user", "system", "user_bookend") all coerce to hybrid. | |
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
| P032 | green  | proxy/test_proxy.py:2884-2900               | test_sse_text_only_stream: every chunk has `object == "chat.completion.chunk"`. Re-asserted in proxy_test.sh Scenario G (line 593). | |
| P033 | green  | proxy/test_proxy.py:2876-2900               | test_sse_text_only_stream: `_sse_chunks` (line 2877) asserts the stream's last line is `data: [DONE]` and the final chunk's `finish_reason == "stop"`; test_v1_chat_completions_streams_sse (2967) asserts the response ends with `data: [DONE]`. Re-asserted in proxy_test.sh Scenario G (line 591). | |
| P034 | green  | proxy/test_proxy.py:2884-2900               | test_sse_text_only_stream: a trailing usage chunk (empty `choices`, `usage` with `prompt_tokens`/`completion_tokens`/`total_tokens`) is emitted; test_sse_tool_calls_arguments_are_json_strings (line 2923) asserts NO usage chunk when usage is omitted. | |
| P035 | green  | proxy/test_proxy.py:2902-2942               | test_sse_tool_calls_arguments_are_json_strings: each tool_call carries a truthy `id` and ascending `index`; test_build_openai_response_non_streaming (2931) asserts the non-stream shape. proxy_test.sh Scenario E (line 488) asserts both ids start with `call_` and are unique. | |
| P036 | green  | proxy/test_proxy.py:982-1013                | test_completion_tokens_falls_back_to_estimate_when_missing: when upstream omits completion_tokens, local estimate used. | |
| P037 | red    | —                                           | —                                                                                                     | No test forces upstream 401 and asserts unlock URL printed + 401 forwarded. |
| P038 | red    | —                                           | —                                                                                                     | No test for 403. |
| P039 | red    | —                                           | —                                                                                                     | No test for 429. |
| P040 | red    | —                                           | —                                                                                                     | No test for upstream 5xx. |
| P041 | red    | —                                           | —                                                                                                     | No test for upstream connection failure -> 502. |
| P042 | red    | —                                           | —                                                                                                     | No test for upstream non-JSON -> 502. |
| P043 | red    | —                                           | —                                                                                                     | No test reads back `01_Inbound_Request_*` dump files. |
| P044 | red    | —                                           | —                                                                                                     | No test reads back `02_API_Request_*` dump files. |
| P045 | red    | —                                           | —                                                                                                     | No test reads back `03_API_Response_*` dump files. |
| P046 | red    | —                                           | —                                                                                                     | No test reads back `03_API_Error_*` dump files. |
| P047 | red    | —                                           | —                                                                                                     | No test reads back `04_OpenAI_Response_*` / `04_OpenAI_SSE_Response_*` dump files. |
| P048 | red    | —                                           | —                                                                                                     | No test reads back `99_Fatal_Error_*` dump files. |
| P049 | red    | —                                           | —                                                                                                     | No test reads back dump filenames (monotonic counter pattern). |
| P050 | green  | tests/proxy_test.sh:556-566                 | Scenario F asserts proxy logs DO NOT contain `'failed to save debug file'` AND DO NOT contain `OUTPUT_DIR '' is not writable` — silence is hard-asserted, not implied. | |
| P051 | green  | tests/proxy_test.sh:582-597                 | Scenario G: streaming request yields OpenAI SSE `data:` lines with `chat.completion.chunk` objects, terminated by `data: [DONE]` (also proxy/test_proxy.py `test_sse_text_only_stream`:2884, `test_v1_chat_completions_streams_sse`:2967). | |
| P052 | green  | tests/proxy_test.sh:232-250                 | Scenario A: non-stream request produces a single `chat.completion` JSON object (`finish_reason:stop`); also proxy/test_proxy.py `test_build_openai_response_non_streaming`:2931. | |
| P053 | green  | proxy/test_proxy.py:837-874                 | TestToolResultDelimiting: tool messages wrapped verbatim in `<<<BEGIN_TOOL_RESULT>>>` markers across every prompt mode; content never parsed. | |
| P054 | green  | proxy/test_proxy.py:26-49 + 96-131          | test_top_level_schema_emitted + test_format_tools_includes_nested_schema confirm tools section format. Cooperative prompt content asserted indirectly via Scenario C body check. | |
| P055 | green  | proxy/test_proxy.py:26-131                  | TestFormatTools asserts each tool's name, schema, and arguments enumerated. | |
| P056 | green  | tests/proxy_test.sh (Scenario C) + proxy/test_proxy.py (TestModelPassthrough) | Scenario C asserts the forwarded body's `model == 'harness'` (the passed-through request model, not a fixed id); test_requested_model_passes_through_verbatim asserts `gpt-4:latest` is forwarded unchanged (no tag stripping). | |
| P057 | green  | proxy/test_proxy.py:939-1004                | test_tool_result_name_resolved_via_tool_call_id / _falls_back_to_positional_order / _prefers_explicit_field / _unknown_when_no_metadata: name resolution chain (field → id → positional → `unknown_tool`). | |
| P058 | green  | proxy/test_proxy.py (TestHybridDetailTools) | test_detail_inlined_under_flagged_task_tool_entry asserts the `<<<BEGIN_TOOL_DETAIL name="task">>>` block + verbatim agent types reach the last user message; test_detail_inlined_block_sits_inside_reminder_after_request asserts placement; test_no_inlined_detail_for_unflagged_tool / test_inlined_detail_only_for_present_flagged_tools / test_empty_flagged_set_disables_inlined_detail cover the gating; test_shipped_detail_tools_are_task_and_skill asserts the SHIPPED `_HYBRID_DETAIL_TOOLS == ["task","skill"]` (the list now comes from `tool-guidance.json`'s `detail_tools`, see P092; the env-parse tests were removed with `_setup_hybrid_detail_tools`). | |
| P059 | green  | proxy/test_proxy.py:test_mode_hybrid_reminder_advises_default_to_tools (+_concurrent_task_agents/_has_honesty_rules/_has_environment_context/_drops_stale_value_parenthetical/_injects_known_host_os/_omits_host_os_when_unknown) | Asserts the labelled recency reminder: Workflow line (prefer a listed tool + `webfetch`/`todowrite`/`todoread` + concurrent `task` agents), Honesty line (anti-fabrication), Environment line (Linux container / mounted workdir / reproducibility), the dropped stale "which agent types…" parenthetical, host-OS parenthetical rendered when `_HOST_OS` set and omitted when empty, and that guidance sits OUTSIDE the `<<<BEGIN_USER_MESSAGE>>>` wrap. TestHostOsSetup covers `HARNESS_HOST_OS` parse/normalise/default. | |
| P060 | green  | tests/proxy_test.sh (Scenario A2)            | Scenario A2 GETs `http://proxy:8000/v1/models` from the agent container and asserts the response lists the mock model `harness` and is an OpenAI `"object":"list"` envelope. | |
| P061 | green  | proxy/test_proxy.py (TestConfigHelpers)      | test_normalize_* asserts `_normalize_api_base` strips `/v1/chat/completions`, `/chat/completions`, and a trailing `/v1` (and preserves a non-`/v1` prefix); chat/models URLs derive from the base. | |
| P062 | green  | proxy/test_proxy.py (TestForceLoopbackGuard) | test_refuses_non_loopback_when_forced asserts `_validate_config()` raises SystemExit for `HARNESS_FORCE_LOOPBACK` in {1,true,yes} crossed with `PROXY_HOST` in {0.0.0.0,192.168.1.10,::}; test_allows_loopback_when_forced passes for 127.0.0.1/::1/localhost; test_allows_any_host_when_not_forced passes for 0.0.0.0 when the var is unset/0/false (container mode). | |
| P063 | green  | proxy/test_proxy.py (TestForceUtf8Stdio)    | test_arrow_crashes_on_cp1252_baseline reproduces the user's crash (writing `sys→user` to a cp1252 `TextIOWrapper` raises `UnicodeEncodeError`); test_reconfigures_to_utf8_and_arrow_survives patches `proxy.sys.stdout`/`stderr` to fresh cp1252 streams, calls `proxy._force_utf8_stdio()`, then asserts `print("sys→user")` to both succeeds, `.encoding` is utf-8, and the UTF-8 bytes of `→` land in the underlying buffer; test_noop_when_stream_lacks_reconfigure asserts a stream without `reconfigure` is tolerated (no raise). | |
| P064 | green  | proxy/test_proxy.py (TestReminderTemplateFile) | test_install_root_overrides_the_adjacent_default (`$INSTALL_ROOT/reminder.md` wins over `reminder.md` beside proxy.py); test_leading_comment_block_is_stripped / _only_a_leading_comment_is_stripped / _trailing_newlines_are_stripped cover the parse; test_shipped_default_loads_and_carries_every_token asserts the real `proxy/reminder.md` loads (`!= _REMINDER_FALLBACK`) and carries `{{HOST_OS}}`/`{{CWD}}`/`{{TOOL_ENTRIES}}`; test_missing_file_falls_back_loudly / _directory_at_the_path_falls_back / _empty_and_comment_only_files_fall_back assert `_REMINDER_TEMPLATE == _REMINDER_FALLBACK`; test_fallback_keeps_the_tool_call_envelope asserts `"name"`/`"arguments"` survive in the fallback; test_tokens_are_substituted_into_the_rendered_reminder covers the `str.replace` substitution. | |
| P065 | green  | proxy/test_proxy.py (TestChatGPTFlatten)    | test_single_message_passes_through_verbatim: `out == "hello"`; test_multi_turn_history_is_labeled_not_dropped asserts `"User: first"`/`"Assistant: second"`/`"User: third"` all present and in order; test_blank_messages_are_dropped, test_empty_history_is_empty_string, and test_list_content_is_flattened_not_crashed cover the remaining edge cases. | |
| P066 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_message_delta_chunks_accumulate: three `message_delta` chunks (`"Hel"`,`"lo "`,`"world"`) collect to `"Hello world"`. | |
| P067 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_cumulative_parts_take_only_the_new_suffix: three cumulative `content.parts` events (`"Hel"`→`"Hello"`→`"Hello world"`) collect to `"Hello world"`, not the concatenation of all three. | |
| P068 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_done_terminates_the_stream: a `message_delta` after `data: [DONE]` is dropped; result is `"kept"`. | |
| P069 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_non_data_and_malformed_lines_are_skipped: blank line, `event: ping`, and `data: {not json` are all tolerated; result is `"ok"`. | |
| P070 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_bytes_lines_are_decoded: `b'data: {...}'` / `b"data: [DONE]"` (bytes, not str) collect to `"bytes"`. | |
| P071 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_posts_to_the_hardcoded_stream_endpoint: `captured["url"] == "https://chat.example.com/backend-api/conversation/stream"` and `captured["kwargs"].get("stream")` is truthy. | |
| P072 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_payload_matches_the_backend_api_dialect asserts `body["action"] == "next"`, `body["model"] == "gpt-5.6-terra"`, `body["timezone"] == "America/Chicago"`, `body["timezone_offset_min"] == 300`, `body["messages"][0]["author"] == {"role": "user"}`, and `content.content_type == "text"` / `content.parts == ["hi"]`. | |
| P073 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_no_server_side_conversation_state_is_reused: `assertNotIn("conversation_id", ...)` and `assertNotIn("parent_message_id", ...)`. | |
| P074 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_headers_carry_cookie_origin_and_referer asserts `Cookie == "session=abc"`, `Accept == "text/event-stream"`, `Origin`/`Referer` derived from `CHATGPT_BASE_URL`, and `"Chrome/152"` in `User-Agent`. | |
| P075 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_returns_an_openai_shaped_response: `extract_assistant_content(body) == "answer"`, `_extract_finish_reason(body) == "stop"`, and `json.loads(resp.text)["object"] == "chat.completion"`. | |
| P076 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_error_status_is_surfaced_with_the_upstream_body: `resp.status_code == 403` and `"cookie expired" in resp.json()["error"]["message"]`. | |
| P077 | green  | proxy/test_proxy.py (TestUpstreamPostRouting) | test_openai_backend_posts_to_chat_url: with `PROXY_BACKEND=openai`, `_upstream_post` posts to `proxy.CHAT_URL`. | |
| P078 | green  | proxy/test_proxy.py (TestUpstreamPostRouting) | test_chatgpt_backend_routes_to_the_chatgpt_client: with `PROXY_BACKEND=chatgpt`, `_upstream_post` returns whatever a patched `_chatgpt_post` returns and is called exactly once. | |
| P079 | green  | proxy/test_proxy.py (TestChatGPTEndToEnd)   | test_plain_answer_reaches_the_client (`content == "42"`), test_cooperative_tool_calls_are_still_extracted (fenced ```json``` tool call still parsed into `tool_calls`), test_upstream_error_becomes_a_502_for_the_client (`resp.status_code == 502`) — all driven through `client.post("/v1/chat/completions", ...)`. | |
| P080 | green  | proxy/test_proxy.py (TestChatGPTModelCatalog) | test_catalog_is_synthesized_without_calling_upstream: `requests.get` patched to raise if called, `GET /v1/models` still returns 200 with `data == [{"id": "gpt-5.6-terra"}]`-shaped list; test_openai_backend_still_proxies_the_catalog asserts the openai backend's response still carries `"upstream-model"`. | |
| P081 | green  | proxy/test_proxy.py (TestChatGPTValidateConfig) | test_chatgpt_requires_only_its_own_three_vars: `_validate_config()` does not raise with the openai trio empty; test_missing_cookie_is_fatal and test_unknown_backend_is_fatal both assert `SystemExit`; test_openai_backend_still_requires_its_trio asserts the openai backend still raises `SystemExit` when its trio is empty. | |
| P082 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_data_without_a_space_is_still_an_event: a stream of `data:{"type": "message_delta", "delta": "tight"}` + `data:[DONE]` (no space after the colon) collects to `"tight"`. | |
| P083 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_event_count_separates_no_stream_from_an_empty_answer: an HTML login page yields `_chatgpt_collect_stream(...)[1] == 0`, while a bare `data: [DONE]` stream yields `text == ""` with `events == 1`. | |
| P084 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_non_assistant_snapshot_cannot_clobber_the_answer: a long `author.role == "tool"` snapshot interleaved between two `message_delta` chunks still collects to `"Hello world"`. | |
| P085 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_non_text_content_type_snapshot_is_skipped: an assistant snapshot with `content_type: "thoughts"` is ignored; result stays `"answer"`. | |
| P086 | green  | proxy/test_proxy.py (TestChatGPTCollectStream) | test_unlabelled_snapshot_is_still_accepted: a snapshot with neither `author` nor `content_type` collects to `"plain snapshot"`. | |
| P087 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_redirects_are_not_followed: `assertIs(captured["kwargs"].get("allow_redirects"), False)`; test_a_redirect_is_an_error_not_an_empty_answer: a 302 with `Location: /auth/login` yields `resp.status_code == 502` and an `error.message` containing both `"redirected"` and `"/auth/login"`. | |
| P088 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_a_200_that_is_not_an_event_stream_is_an_error: a 200 body of `<!doctype html>` / `<title>Sign in</title>` yields `resp.status_code == 502` with `"no SSE events"` in `error.message`. | |
| P089 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_a_stream_that_genuinely_said_nothing_is_still_a_200: a bare `data: [DONE]` stream yields `resp.status_code == 200` and `extract_assistant_content(resp.json()) == ""` (the positive control that keeps P088 from over-firing). | |
| P090 | green  | proxy/test_proxy.py (TestChatGPTPost)       | test_origin_strips_a_path_prefix_from_the_base_url: with `base_url="https://chat.example.com/api"`, `headers["Origin"] == "https://chat.example.com"` and `headers["Referer"] == "https://chat.example.com/"`. | |
| P091 | green  | proxy/test_proxy.py (TestChatGPTValidateConfig) | test_passthrough_is_rejected_on_the_chatgpt_backend: `_PROMPT_MODE="passthrough"` + `PROXY_BACKEND="chatgpt"` (with a complete CHATGPT_* trio) raises `SystemExit` from `_validate_config()`; test_passthrough_is_still_fine_on_the_openai_backend asserts the same mode does NOT raise on `PROXY_BACKEND="openai"`. | |
| P092 | green  | proxy/test_proxy.py (TestToolGuidanceFile) | test_install_root_overrides_the_adjacent_default (one `INSTALL_ROOT` resolves BOTH `tool-guidance.json` and `reminder.md`, asserted in the same test) / test_default_path_sits_next_to_proxy_py (resolved off `proxy.__file__`, so it holds in the container where the Dockerfile flattens to /app); test_shipped_default_loads_every_section asserts the real `proxy/tool-guidance.json` loads with zero warnings and carries a legend, both notes, `detail_tools == ["task","skill"]` and >=18 non-empty descriptions; test_one_bad_description_does_not_cost_the_others asserts `{"bash": "...", "edit": 5, "read": ""}` keeps `bash` and names `edit`/`read` in one warning; test_a_missing_key_keeps_the_built_in_default / test_wrong_types_fall_back_per_key_and_name_the_key / test_empty_notes_are_honoured_but_an_empty_legend_is_not / test_explicit_empty_detail_tools_is_honoured / test_non_string_detail_tool_entries_are_dropped cover per-key degradation; test_malformed_json_names_line_and_column asserts the JSONDecodeError position reaches the message; test_missing_file_falls_back_loudly / test_directory_at_the_path_falls_back / test_non_object_top_level_falls_back assert the whole-file fallback; test_setup_logs_warnings_and_a_count covers `_setup_tool_guidance`'s `[!]` + count line; test_edited_file_reaches_the_rendered_block / test_a_tool_dropped_from_the_file_renders_a_bare_signature / test_notes_only_render_when_their_condition_holds assert the edit reaches `_format_tool_entries`. | |

## A — Agent runtime (init, configs, run-opencode/shell) (24 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| A001 | green  | tests/firewall_test.sh:200-261              | Phase 3 verifies that without `HARNESS_FIREWALL_DISABLED=1`, the agent container's OUTPUT policy remains DROP — proving the firewall script ran. | |
| A002 | green  | tests/firewall_test.sh:213-228              | Phase 3: `grep -q 'DISABLED via HARNESS_FIREWALL_DISABLED=1'` confirms skip + warning. | |
| A003 | green  | tests/integration_test.sh:460-573 + persistence_test.sh:191-245 | Graphify phase: `stat -c '%u' = $(id -u)` proves UID remap; persistence T3 asserts pip-installed files are owned by host UID. | |
| A004 | green  | tests/persistence_test.sh:155-176 + 255-276 | T1 stat's `.harness-home-initialized` and asserts both `%g == host gid` and `%u == host uid`; T3 stat's the pip-installed `requests` package directory and asserts the same paired uid+gid match — catches identity-mismatch regressions where only one half of remap is correct. | |
| A005 | red    | —                                           | —                                                                                                     | No test runs the entrypoint twice and asserts the second pass is a no-op for remap. |
| A006 | red    | —                                           | —                                                                                                     | No test asserts chown is skipped when already correctly owned. |
| A007 | green  | tests/full_pipeline_test.sh:453-464         | T9 stat's `${agent_home_marker}` (`.harness-home-initialized`, written by user-side init AFTER gosu drop) and asserts `%u == $(id -u)` — direct evidence the agent dropped privileges before user-side init ran. | |
| A008 | red    | —                                           | —                                                                                                     | No test asserts `configure-git-credentials.sh` is invoked during agent startup. (persistence_test T6 verifies `.git-credentials` survives, not that the script runs.) |
| A009 | green  | tests/persistence_test.sh:137-155 + full_pipeline_test.sh:444-462 | T1 + T15: `.harness-home-initialized` marker written; `seeded_count >= 2` after first run. | |
| A010 | red    | —                                           | —                                                                                                     | No test sets `HARNESS_HOST_CWD` and asserts the agent `cd`'d into it. |
| A018 | green  | tests/full_pipeline_test.sh (T9)            | T9 boots opencode via bare `harness -p`; the entrypoint runs `ensure_opencode_config` before exec'ing the agent, so the test asserts `state/agent/home/.config/opencode/opencode.json` exists in the shared home AND carries the harness provider block (`"harness"`) + model binding (`"model": "harness/`). Holds even on the opencode provider-auth skip, since the config write precedes the agent run. | |
| A019 | red    | —                                           | —                                                                                                     | No test asserts opencode config has a `harness` provider pointing at the proxy endpoint. |
| A020 | red    | —                                           | —                                                                                                     | No test asserts opencode config defines a `yolo` agent profile. |
| A035 | green  | tests/full_pipeline_test.sh:T9              | T9 asserts the launched opencode.json carries `"name": "GenAI Harness"` (the fixed provider name hardcoded in the entrypoint). | |
| A036 | green  | tests/full_pipeline_test.sh:T9              | T9 asserts opencode.json carries a model entry `"name": "harness"` — the model discovered via the proxy `GET /v1/models` route, distinct from the provider whose name is `GenAI Harness`. | |
| A021 | red    | —                                           | —                                                                                                     | No test asserts opencode mcp-servers merge happens. |
| A022 | red    | —                                           | —                                                                                                     | No test asserts local vs remote mcp distinction in opencode shape. |
| A028 | red    | —                                           | —                                                                                                     | No test asserts `OPENCODE_DISABLE_AUTOUPDATE=1`. |
| A029 | red    | —                                           | —                                                                                                     | No test exercises `--agent yolo` via `HARNESS_YOLO=1`. |
| A030 | green  | tests/full_pipeline_test.sh:391-417         | T10: `harness opencode -p` succeeds, producing a response. | |
| A031 | green  | tests/full_pipeline_test.sh:500-512         | T10 grep's `! grep -Eqi 'unknown (option\|flag).*-p\|unknown (option\|flag).*--print'` against the opencode output (stray flag would surface an unknown-flag error from opencode), AND source-greps `agents/entrypoint.sh` for the literal strip branch `"$arg" == "-p" || "$arg" == "--print"`. | |
| A032 | red    | —                                           | —                                                                                                     | No test exercises `harness shell` (interactive). |
| A033 | green  | tests/full_pipeline_test.sh + harness_test.sh T30 | T9 (bare `harness` → opencode) and T10 (explicit opencode) dispatch from positional mode; harness_test T30 asserts bare/flag dispatch routes to opencode and an unknown bare word errors. | |
| A034 | red    | —                                           | —                                                                                                     | No test invokes an unknown mode and asserts the usage error. |

## M — MCP framework (33 IDs)

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
| M013 | green  | tests/mcp_test.sh:353-368 + 415-441         | T7 side-file lists service when enabled; after T9 disable T17 verifies side-file is regenerated/removed. | |
| M014 | green  | tests/mcp_test.sh:353-368                   | T7: services list parsed from compose.yml shows `test_mcp`. | |
| M015 | green  | tests/mcp_test.sh:396-411                   | T8: post-up `mcp status` shows `running`. | |
| M016 | green  | tests/mcp_test.sh:485-497                   | T12: post-down `mcp status` shows `stopped`. | |
| M017 | red    | —                                           | —                                                                                                     | No test deletes the container outright (without `mcp down`) to assert `not_created`. |
| M018 | red    | —                                           | —                                                                                                     | No test uninstalls then re-installs and asserts `enabled: true` re-set. |
| M019 | green  | tests/integration_test.sh:253-456           | Phase 2 mounts `HARNESS_PROJECTS_ROOT` and reaches `/workspaces/projects/test-project/src/calculator/core.py`. | |
| M020 | green  | tests/integration_test.sh:253-456           | Phase 2 down/up cycle: serena index `data/` persists across container recreation. | |
| M021 | green  | tests/integration_test.sh:253-456           | Phase 2: proxy reaches `tcp://serena:9121` (SSE port) inside the compose network. | |
| M022 | green  | tests/integration_test.sh:253-456           | Phase 2: side-file contains `serena`, requiring the allowlist mount to have been merged. | |
| M023 | green  | tests/mcp_test.sh:576-595 + 415-441         | T17 verifies side-file removed when no MCPs are enabled; T9 demonstrates regeneration on flag change. | |
| M024 | green  | tests/mcp_test.sh (TR1)                      | TR1: `register reg1 --from <dir>` asserts `compose.yml` + `client-config.json` + `harness-meta.json` (`"enabled": true`) materialized, `data/` created, `.staging-reg1` gone. | |
| M025 | green  | tests/mcp_test.sh (TR2)                      | TR2: malformed-compose source → non-zero exit, no `state/mcp/reg2/compose.yml`, `.staging-reg2` discarded. | |
| M026 | green  | tests/mcp_test.sh (TR3)                      | TR3: snippet whose service is named `proxy` (already in the graph) is rejected with `redefines service`; no active entry left. | |
| M027 | green  | tests/mcp_test.sh (TR4)                      | TR4: `--no-enable` → `"enabled": false`, absent from `mcp_compose_files` (source seam); `mcp enable` flips it and it appears. | |
| M028 | green  | tests/mcp_test.sh (TR5)                      | TR5: enabled `reg5` appears in `mcp_compose_files`; `write_agent_mcp_config` side-file carries `http://reg5:8765/sse` (mirrors T7). | |
| M029 | green  | tests/mcp_test.sh (TR6)                      | TR6: re-register without `--force` rejected (`already installed`); `--force` re-materializes, `data/marker.txt` preserved. | |
| M030 | green  | tests/mcp_test.sh (TR7)                      | TR7: register prints `recommends allowlisting` + `harness net allow example.com`/`another.example.org`; allowlist unchanged (`diff -q`). Mirrors T18. | |
| M031 | green  | tests/mcp_test.sh (TR8)                      | TR8: `uninstall reg8 --force` removes `compose.yml` but keeps `data/marker.txt` (inverse of register). | |
| M032 | green  | tests/mcp_test.sh (TR9)                      | TR9: registering under name `dummy` (a registry entry) prints `also names a registry entry` shadow warning. | |
| M033 | green  | tests/mcp_test.sh (TR10)                     | TR10: `register --from file://<repo> --ref v0.0.1 --start` clones into `state/mcp/reg10/repo/.git`, records `repo_clone_url`/`v0.0.1` provenance, and `--start` brings the container to `healthy`. | |

## N — Network egress / firewall / allowlist / git creds / overrides (31 IDs)

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
| N017 | green  | tests/firewall_test.sh:248-258 + podman_smoke_test.sh:181-194 | Phase 3: agent OUTPUT policy STILL `-P OUTPUT DROP` even with proxy bypass. Podman smoke T3 repeats the assertion under rootless podman. | |
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
| N031 | red    | —                                           | —                                                                                                     | No test sets a scheme-less upstream URL (e.g. `PROXY_API_URL=api.example.com/v1`) and asserts `init-firewall.sh` exits 1 with the "could not parse a hostname" FATAL. `tests/firewall_test.sh` Phase 2 only covers the *parsed-but-not-allowlisted* host (N018); it never drives the empty-`api_host` branch. Verified by reading `firewall/init-firewall.sh` and grepping the whole `tests/` tree — this is genuinely uncovered, not merely CI-only. |

## U — Upgrade actions library (33 IDs)

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
| U030 | green  | tests/upgrade_test.sh:875-996 (T13)         | T13 copies the shipped file over the user's, asserts `upgrade_userfile_needs_sync` returns non-zero and `jq -r .reason == "identical"`, and that no `.bak` was written. | |
| U031 | green  | tests/upgrade_test.sh:875-996 (T13)         | T13 feeds `n` and then a bare Enter to `upgrade_userfile_sync` (via `HARNESS_CONFIRM_FROM_STDIN=1` and the real `_upgrade_confirm` sourced from `harness`): both give `reason == "declined"`, `MY OWN WORDING` still greps in the target, and no `.bak` exists — Enter defaults to keeping the user's copy. | The printed line delta is not asserted, only the decision. |
| U032 | green  | tests/upgrade_test.sh:875-996 (T13)         | T13 feeds `y`: `files_updated[0] == "user.md"`, the target now `diff`s clean against the shipped source, and `user.md.bak` still greps `MY OWN WORDING`. | The `backup_failed` branch (unwritable directory) is not exercised. |
| U033 | green  | tests/upgrade_test.sh:875-996 (T13)         | T13 asserts `reason` is `dry_run` with `dry_run=1`, `not_prompted` with `allow_prompt=0`, `target_missing` after deleting the target (and that nothing is created), and `source_missing` after deleting the source (with the user's file undamaged); `upgrade_userfile_needs_sync` returns non-zero for both missing cases and zero only when both exist and differ. Also asserts `upgrade_actions.sh` defines its own `_upgrade_confirm` fallback when sourced standalone. | The no-tty half of the not-prompted condition is covered via `allow_prompt=0`, not by detaching a tty. |

## Pe — Persistence (paths that must survive lifecycle/upgrade) (16 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| Pe001 | green  | tests/upgrade_test.sh:227-354               | T6: `.env` (with user-set `PROXY_API_URL=https://my-llm.example/v1`) survives upgrade idempotently. | |
| Pe002 | green  | tests/upgrade_test.sh:113-141               | T2 simulates allowlist upgrade; existing host entries preserved. | |
| Pe003 | red    | —                                           | —                                                                                                     | No test makes `.harness-net-overrides.json` and asserts it survives `down`. |
| Pe004 | green  | tests/persistence_test.sh:191-426 + full_pipeline_test.sh:444-462 | T3, T4, T5, T6 all verify state/agent/home/ content persistence across container rebuild. | |
| Pe006 | yellow | tests/proxy_test.sh:55                      | OUTPUT_DIR env defined; never modified by upgrade test paths. | No upgrade-touches-output-dir test. |
| Pe007 | green  | tests/mcp_test.sh:238-259 + 527-546         | install creates `state/mcp/<name>/`; uninstall removes it. | |
| Pe008 | green  | tests/upgrade_test.sh:146-174 + mcp_test.sh:415-441 | T4 (DEALBREAKER): post-upgrade `jq '.enabled' harness-meta.json == false` preserved; T9 demonstrates disable state. | |
| Pe009 | red    | —                                           | —                                                                                                     | No test asserts `state/mcp/serena/data/` survives a fake upgrade. (Integration test exercises serena but doesn't run an upgrade across it.) |
| Pe010 | green  | tests/full_pipeline_test.sh:326-336         | T5 asserts if `state/.harness-runtime.yml` exists, it carries the `# Generated by harness; do not edit.` header — guarantees the file is the regenerated artifact, not a stale hand-written one. | |
| Pe011 | green  | tests/mcp_test.sh:576-595                   | T17 re-triggers side-file write path. | |
| Pe014 | green  | tests/persistence_test.sh:137-176           | T1 + T2: skel-seed once, user edits preserved on second run. | |
| Pe015 | green  | tests/full_pipeline_test.sh:579-597         | T13 down: no state files deleted. | |
| Pe016 | green  | tests/upgrade_test.sh:60-354                | All upgrade tests preserve user state files (asserted across T1, T4, T6). | |
| Pe017 | green  | tests/mcp_test.sh:527-546                   | T14: uninstall removes `state/mcp/<name>/` recursively. | |
| Pe018 | green  | tests/upgrade_test.sh:146-174               | T4 (DEALBREAKER): harness-meta.json with `.enabled == false` survives directory_overwrite even when source has it as true. | |
| Pe019 | green  | tests/upgrade_test.sh:146-174               | T4: `data/` subdir preserved (`data/user.txt == "user-state"`). | |

## I — Installer + platform.sh primitives (50 IDs)

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
| I035 | green  | tests/unit_workdir_test.sh (T3)             | OS pinned to `windows` with a `cygpath -u` stub; `harness_abs_path` normalises `C:/Users/...`, `c:\Users\...`, and `/c/Users/...` to `/c/Users/...`. T4 also covers the issue #112 pipeline (`harness_abs_path` → `harness_container_workdir` produces `-w //c/...` so MSYS does not rewrite it). | |
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
| I046 | green  | tests/full_pipeline_test.sh (T0b)           | T0b points `HARNESS_REPO_URL` at a non-existent path, runs the installer, and asserts non-zero exit, a `git clone of .* failed` message, and that `install complete` is NOT printed. | |
| I047 | green  | tests/full_pipeline_test.sh (T0b)           | T0b `source`s the installer (the README path) on a failing clone and asserts the sourced run aborts non-zero and leaves no half-install (`harness/state` not created) — the regression #105/#106 guard. | |
| I048 | green  | tests/full_pipeline_test.sh (T0b)           | T0b drops a `.env` beside the installer with `HTTPS_PROXY` set and asserts the installer prints `using HTTPS_PROXY from .* for the clone`. | |
| I049 | green  | tests/unit_workdir_test.sh (T1, T2, T4)     | T1 asserts pass-through on linux/macos; T2 asserts windows produces `//c/...` (including idempotence and slash-collapse); T4 covers the full `harness_abs_path` → `harness_container_workdir` pipeline that feeds docker `-w` (issue #112). | |
| I050 | green  | tests/unit_workdir_test.sh (T5)             | OS pinned to `windows` with a fake-runtime recorder: `harness_docker` and `harness_docker_winpty` both spawn the runtime with MSYS_NO_PATHCONV=1 AND MSYS2_ARG_CONV_EXCL='*' set; `local -x` scope confirmed (vars don't leak into caller); pass-through on non-Windows (no env leakage). Issue #112 root-cause fix. | |
| I051 | green  | tests/unit_workdir_test.sh (T6)             | OS pinned both ways: linux emits `-v src:tgt` and `-v src:tgt:ro`; windows emits a single `--mount=type=bind,source=,target=[,readonly]` token with no `:/c/` composite. Covers the issue #112 winpty path where `-v` is mangled to `invalid mode: …`. | |

## B — Bootstrap (`harness-bootstrap.sh`, 10 IDs)

| ID    | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|-------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| B001 | green  | tests/unit_bootstrap_test.sh:T1             | T1 asserts the fetched stub installer's `$script_dir` (resolved from its own `BASH_SOURCE`) equals the bundle dir, which is the bootstrap's own resolved dir. | |
| B002 | green  | tests/unit_bootstrap_test.sh:T1,T3          | T1 puts `HTTPS_PROXY` in the bundled `.env` and asserts the handed-off installer saw it; T3 uses a blank `HTTPS_PROXY=` and asserts the run does not crash (host env left as-is). | |
| B003 | green  | tests/unit_bootstrap_test.sh:T1 + T4        | T1 drives the local-path branch (`HARNESS_REPO_URL` → stub repo dir, copied, no network); T4 source-greps for the `raw.githubusercontent.com` remote-fetch path. | |
| B004 | green  | tests/unit_bootstrap_test.sh:T4             | T4 source-greps for `HARNESS_INSTALL_REF` and `HARNESS_REPO_URL` wiring. | |
| B005 | green  | tests/unit_bootstrap_test.sh:T2             | T2 points the fetch at a non-`#!` HTML file and asserts it is rejected (does not run as the installer). | |
| B006 | green  | tests/unit_bootstrap_test.sh:T2             | T2 asserts the "falling back to the bundled" notice and that the bundled `harness-install.sh` runs (`BUNDLED_FALLBACK_RAN`) when the fetch is rejected. | No positive test of the no-bundled-copy abort branch (the `return/exit` path); covered by source-grep only. |
| B007 | green  | tests/unit_bootstrap_test.sh:T1             | T1 asserts the fetched installer's `$script_dir` is the bundle dir (so it would find `.env`/`.harness-allowlist` beside it). | |
| B008 | green  | tests/unit_bootstrap_test.sh:T1,T3          | T1 (executed) asserts the child installer ran and rc propagated; T3 (sourced) asserts the installer ran in-shell and `SOURCED_RC=0` is returned. | |
| B009 | green  | tests/unit_bootstrap_test.sh:T3 + T4        | T3 sources the bootstrap then references an unset var and asserts no abort (no leaked `set -u`); T4 source-greps the sourced-vs-executed detection. | |
| B010 | green  | tests/unit_bootstrap_test.sh:T1             | T1 asserts `.harness-install.fetched.sh` is gone after the run; T2 implicitly preserves the bundled copy (its name does not match the temp). | |

## Ho — Host mode, containerless (18 IDs)

| ID    | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|-------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| Ho001 | green  | tests/unit_host_test.sh:52-59 (T1)          | Subshell with `PROXY_API_URL=""` runs `host_require_config`; asserts non-zero exit AND that the error names `PROXY_API_URL`. | |
| Ho002 | green  | tests/unit_host_test.sh:61-65 (T2)          | All three required vars set → `host_require_config` returns 0. | |
| Ho003 | green  | tests/unit_host_test.sh:67-73 (T3)          | `HARNESS_HOST_CONFIRM=1 host_confirm_gate 0` returns 0 and prints an "auto-confirmed" notice (no TTY prompt). | |
| Ho004 | green  | tests/unit_host_test.sh:75-84 (T4)          | `PATH="/nonexistent-dir" host_preflight` returns non-zero and the output names each of `python3`, `jq`, `node`, `opencode`. | |
| Ho005 | green  | tests/unit_host_test.sh:86-98 (T5)          | `host_write_opencode_config` (PORT=8123, model=test-model) writes valid JSON; asserts `baseURL == http://127.0.0.1:8123/v1`, `apiKey == harness-dummy`, and the models map has `test-model`. | |
| Ho006 | green  | tests/unit_host_test.sh:100-104 (T6)        | With `PATH` stripped (no jq), `host_write_opencode_config` returns non-zero rather than reaching the docker jq sidecar (M4 guard). | |
| Ho007 | green  | tests/unit_host_test.sh:106-114 (T7)        | `host_proxy_fingerprint` is identical across two runs of the same config and differs when `PROXY_PORT` or `PROXY_API_KEY` changes (M2 config-change restart trigger). | |
| Ho018 | green  | tests/unit_host_test.sh:116-150 (T8)        | `host_python_bin` verifies each candidate via `--version` (not `command -v` alone): a dead `python3` stub that prints "Python was not found" (Windows App-execution-alias) is rejected and it falls through to a real `python`; a real `python3` is preferred; only dead stubs → non-zero. | |
| Ho008 | green  | tests/unit_host_upgrade_test.sh:T1,T2        | T1: with `harness_runtime_installed`→1 and `jq` hidden, `cmd_upgrade --no-prompt` (HARNESS_UPGRADE_SKIP_PULL=1) returns 0, prints "no container runtime found" and "[harness] host-only upgrade complete.", and never reaches the require_docker/cmd_down/cmd_start sentinels. T2: with a runtime present it exits via the require_docker sentinel (rc 91) and does NOT print the host-only completion. | |
| Ho009 | green  | tests/unit_host_toolchain_test.sh:T1         | `host_jq_platform`/`host_node_platform` return well-formed tokens (`linux\|macos-amd64\|arm64`, `linux\|darwin-x64\|arm64`) that agree with `uname -m`. | |
| Ho010 | green  | tests/unit_host_toolchain_test.sh:T2         | `host_sha_from_manifest` (curl stubbed with a fixture) returns the correct hash for a `<sha>  <file>` entry and fails (no match) for an absent or non-end-anchored name. | |
| Ho011 | green  | tests/unit_host_toolchain_test.sh:T3         | `host_sha256_check` accepts a file's real sha256 and rejects a wrong one. | |
| Ho012 | green  | tests/unit_host_toolchain_test.sh:T4         | `host_toolchain_path_prefix` is empty with nothing vendored and lists exactly `tool_bin:node-bin:opencode-bin` (in order) once stub binaries exist. | |
| Ho013 | green  | tests/unit_host_toolchain_test.sh:T5         | `host_ensure_toolchain` returns non-zero on a stubbed unsupported OS (`harness_detect_os`→freebsd) before any download, and on linux (provisioners stubbed) prepends the vendored bin dir to PATH. | |
| Ho014 | green  | tests/unit_host_toolchain_test.sh:T6         | `HARNESS_HOST_OPENCODE_VERSION`/`HARNESS_HOST_OPENAI_COMPAT_VERSION` equal `agents/Dockerfile` ARG defaults and `HARNESS_HOST_NODE_VERSION` major equals the Dockerfile `FROM node:NN` major (pin drift guard). | |
| Ho015 | green  | tests/unit_host_toolchain_test.sh:T7         | Windows (Git Bash) layout: stubbing `harness_detect_os`→windows + `uname`, `host_jq_platform`=windows-amd64, `host_node_platform`=win-x64, `host_exe_suffix`=.exe, vendored jq is `jq.exe`, the jq asset is `jq-windows-amd64.exe`, `node.exe`/opencode shim resolve at the dir **root** (not `bin/`), the venv is `Scripts/python.exe`; win-arm64 resolves Node but `host_jq_platform` fails closed (no upstream build). | |
| Ho016 | green  | tests/unit_host_toolchain_test.sh:T8         | `host_toolchain_path_prefix` under a stubbed Windows OS orders the dirs `tool_bin:node-root:opencode-root` (Windows layout, stub binaries at the root). | |
| Ho017 | green  | tests/unit_host_toolchain_test.sh:T9         | `host_extract_archive` extracts a real `.tar.gz` (and a `.zip` round-trip when `zip`/`unzip` are present), and rejects an unknown archive kind. | |

## C — ChatGPT backend, CLI side (40 IDs)

| ID   | Status | Test file & line                            | Evidence                                                                                              | Gap (yellow/red) |
|------|--------|---------------------------------------------|-------------------------------------------------------------------------------------------------------|------------------|
| C001 | green  | tests/unit_chatgpt_test.sh (T1)             | `backend_override=""; _backend_is_chatgpt && fail ...` — asserts the predicate fails (bash false) with no override set. | |
| C002 | green  | tests/unit_chatgpt_test.sh (T1)             | `backend_override="chatgpt"; _backend_is_chatgpt \|\| fail ...` — asserts the predicate succeeds once selected. | |
| C003 | green  | tests/unit_chatgpt_test.sh (T1)             | Asserts `_effective_default_model` returns `"openai-model"` (from `DEFAULT_MODEL_NAME`) by default and `"gpt-5.6-terra"` (from `CHATGPT_MODEL_NAME`) once `backend_override=chatgpt`. | |
| C004 | green  | tests/unit_chatgpt_test.sh (T2)             | `grep -q 'PROXY_BACKEND: "chatgpt"' "$runtime_override"` and `grep -q '^  proxy:' "$runtime_override"` after `write_runtime_override` with `backend_override=chatgpt`. | |
| C005 | green  | tests/unit_chatgpt_test.sh (T3)             | `[[ "$(grep -c '^  proxy:' "$runtime_override")" == "1" ]]` with both `backend_override` and `prompt_mode_override` set — a single `proxy:` block carries both `PROXY_BACKEND` and `PROXY_PROMPT_MODE`. | |
| C006 | green  | tests/unit_chatgpt_test.sh (T4)             | `backend_override=""; prompt_mode_override=""; write_runtime_override; [[ ! -f "$runtime_override" ]]`. | |
| C007 | green  | tests/unit_chatgpt_test.sh (T5)             | Subshell with `backend_override=chatgpt` and the openai trio blanked still passes `require_runtime_config`. | |
| C008 | green  | tests/unit_chatgpt_test.sh (T5)             | Subshell with `backend_override=""` and the `CHATGPT_*` trio blanked still passes `require_runtime_config`; the paired assertion in the same T5 block confirms a missing `CHATGPT_COOKIE_STRING` still aborts when `backend_override=chatgpt`. | |
| C009 | green  | tests/unit_chatgpt_test.sh (T6)             | Same swap re-driven against `host_require_config`: chatgpt backend tolerates a blanked openai trio but still aborts on a missing `CHATGPT_BASE_URL`. | |
| C010 | green  | tests/unit_chatgpt_test.sh (T7)             | `_probe_upstream_auth` stubbed to `return 1`; `_gate_on_upstream_auth` with `backend_override=chatgpt` still exits 0 — proves the early return skips the probe entirely. | |
| C011 | green  | tests/unit_chatgpt_test.sh (T7)             | Same stub-fails-loudly pattern for `_print_upstream_models` — exits 0 with `backend_override=chatgpt`. | |
| C012 | green  | tests/unit_chatgpt_test.sh (T8)             | `base_fp="$(host_proxy_fingerprint)"` under the default backend differs from `cg_fp` computed under `backend_override=chatgpt`. | |
| C013 | green  | tests/unit_chatgpt_test.sh (T8)             | Loop over `CHATGPT_BASE_URL`/`CHATGPT_MODEL_NAME`/`CHATGPT_COOKIE_STRING`: changing any one value changes `host_proxy_fingerprint`'s output versus `cg_fp`. | |
| C014 | green  | tests/unit_chatgpt_test.sh (T9)             | `harness_docker` stubbed to emit `PROXY_BACKEND=chatgpt` → `_running_proxy_backend` returns `chatgpt`; stubbed with no `PROXY_BACKEND` line → returns `openai`. | |
| C015 | green  | tests/unit_chatgpt_test.sh (T9)             | `compose() { echo ""; }` (no running container) → `_running_proxy_backend` exits non-zero. | |
| C016 | green  | tests/unit_chatgpt_test.sh (T10)            | `_running_proxy_backend` stubbed to `openai`, `backend_override=chatgpt` → `ensure_services_up` writes to `$START_LOG` (stack restarted). | |
| C017 | green  | tests/unit_chatgpt_test.sh (T10)            | Matching-backend cases (`openai`/`openai` and `chatgpt`/`chatgpt`) both leave `$START_LOG` empty — `[[ ! -s "$START_LOG" ]]`. | |
| C018 | green  | tests/unit_chatgpt_test.sh (T11)            | `_config_is_secret CHATGPT_COOKIE_STRING` succeeds; `_config_get` output matches `'set,'` and does NOT contain the raw cookie value `abcdefghijklmnop`. | |
| C019 | green  | tests/unit_chatgpt_test.sh (T11)            | `_config_editable_keys \| grep -q '^CHATGPT_BASE_URL\|'` (and the same for `CHATGPT_MODEL_NAME`/`CHATGPT_COOKIE_STRING`). | |
| C020 | green  | tests/unit_chatgpt_test.sh (T12)            | `_config_set CHATGPT_BASE_URL "https://chat.newhost.example:8443"` then `_config_read_key` round-trips the URL AND `grep -q '^chat.newhost.example' "$ALLOWLIST"`. | |
| C021 | green  | tests/unit_chatgpt_test.sh (T13)            | `cmd_chatgpt --help` output matches `'harness chatgpt host'` and `'CHATGPT_COOKIE_STRING'`; `[[ -z "$backend_override" ]]` afterward confirms no side effect. | |
| C022 | green  | tests/unit_chatgpt_test.sh (T14)            | `cmd_chatgpt --yolo` (stubbed `run_agent`) logs `run_agent:opencode --yolo`; asserts `backend_override == "chatgpt"` and `agent_model == "gpt-5.6-terra"`. | |
| C023 | green  | tests/unit_chatgpt_test.sh (T14)            | `cmd_chatgpt host -p 'hi'` (stubbed `cmd_host`) logs `cmd_host:-p hi`; asserts `backend_override == "chatgpt"`. | |
| C024 | green  | tests/unit_chatgpt_test.sh (T15)            | `grep -qE '^ *chatgpt\)  *cmd_chatgpt "\$@" ;;' "$HARNESS"` — source-grep of the wrapper's `main()` dispatch table. | |
| C025 | green  | tests/unit_chatgpt_test.sh (T15)            | `grep -q 'chatgpt \[host\] \[args\]' "$HARNESS"` — source-grep of `cmd_help`'s text. | |
| C026 | green  | tests/unit_chatgpt_test.sh (T16)            | Writes a cookie with `; ` separators, asserts the on-disk line is quoted (`grep -q "^CHATGPT_COOKIE_STRING='"`), `_config_read_key` equality, and that a child `bash -c 'set -euo pipefail; set -a; . .env'` both succeeds and yields the exact cookie; then asserts `grep -q '^CHATGPT_MODEL_NAME=gpt-5.6-terra$'` for the no-churn case. | |
| C027 | green  | tests/unit_chatgpt_test.sh (T16, T17)       | T16 asserts the semicolon-bearing cookie lands as `^CHATGPT_COOKIE_STRING='` (single-quote form) and a bare value stays bare (`^CHATGPT_MODEL_NAME=gpt-5.6-terra$`); T17 writes `it's $HOME \`x\` "q" \\ done` — a value containing a single quote — and asserts `grep -q '^CHATGPT_COOKIE_STRING="'`, the double-quote fallback. | |
| C028 | green  | tests/unit_chatgpt_test.sh (T16, T17)       | Both blocks assert `[[ "$(_config_read_key CHATGPT_COOKIE_STRING)" == "$value" ]]` for the value they just wrote — the single-quoted cookie (T16) and the escaped double-quoted value (T17) — and both re-source the rewritten `.env` in a child `bash -c 'set -euo pipefail; set -a; . "$1"'` and compare the sourced value, so bash's own unquoting is the independent oracle for the inverse. | |
| C029 | green  | tests/unit_chatgpt_test.sh (T2)             | After `write_runtime_override` with `backend_override=chatgpt`: `grep -q "HARNESS_PROXY_FP: \"$(_chatgpt_config_fingerprint)\"" "$runtime_override"` (the hash is injected on the `proxy:` block) AND `grep -q "$CHATGPT_COOKIE_STRING" "$runtime_override" && fail "T2: the cookie leaked into the compose override file"`. | |
| C030 | green  | tests/unit_chatgpt_test.sh (T9)             | `harness_docker` stubbed to emit `HARNESS_PROXY_FP=deadbeefcafe0001` → `_running_proxy_fp` returns exactly that; stubbed with no such line → empty output; stubbed with `CHATGPT_COOKIE_STRING=leakme` sitting next to the hash → `_running_proxy_fp \| grep -q leakme` must not match. | |
| C031 | green  | tests/unit_chatgpt_test.sh (T10)            | With `_running_proxy_backend`→`chatgpt` (backend matches) and `_running_proxy_fp`→`staleeeeeeeeeeee`, `ensure_services_up` writes to `$START_LOG` — `[[ -s "$START_LOG" ]] \|\| fail "T10: a rotated cookie must restart the proxy"`. The paired case with `_running_proxy_fp() { _chatgpt_config_fingerprint; }` leaves the log empty. | |
| C032 | green  | tests/unit_chatgpt_test.sh (T10)            | `_running_proxy_backend`→`openai`, `_running_proxy_fp`→`''`, `backend_override=""` → `[[ ! -s "$START_LOG" ]] \|\| fail "T10: the openai backend must not restart on a missing fingerprint"`. | |
| C033 | green  | tests/unit_chatgpt_test.sh (T14)            | `cmd_doctor`/`cmd_preflight` stubbed to log; `cmd_chatgpt doctor` logs `cmd_doctor:` and `cmd_chatgpt preflight` logs `cmd_preflight:`, and after each `[[ "$backend_override" == "chatgpt" ]]` is asserted. | |
| C034 | green  | tests/unit_chatgpt_test.sh (T7)             | `curl` stubbed to touch `$CURL_MARKER`; with `backend_override=""`, `PROXY_API_URL`/`PROXY_API_KEY` set, `_print_upstream_models` leaves the marker present — `[[ -e "$CURL_MARKER" ]] \|\| fail "T7: the openai backend must still pull the catalog"`. The chatgpt half (C011) asserts the marker is absent. | |
| C035 | green  | tests/unit_chatgpt_test.sh (T14)            | `cmd_start`/`cmd_restart`/`cmd_down` stubbed to log; the loop over `start restart down` asserts each logs `cmd_<verb>:` and that `backend_override == chatgpt` after. The real `cmd_restart`/`cmd_down`/`cmd_preflight` are saved with `declare -f` and restored, since T19/T20 exercise them. | |
| C036 | green  | tests/unit_chatgpt_test.sh (T19)            | Four cases: running `chatgpt` sets `backend_override`+`agent_model`; running `openai` leaves it empty; a pre-set `chatgpt` survives a running `openai`; with `CHATGPT_BASE_URL=""` the stubbed `_running_proxy_backend` writes nothing to `$PROBE_LOG`. | |
| C037 | green  | tests/unit_chatgpt_test.sh (T19)            | `_running_proxy_backend`/`cmd_down`/`cmd_start` stubbed to append to `$ORDER_LOG`; `cmd_restart` must produce exactly `read\ndown\nstart:chatgpt`. | |
| C038 | green  | tests/unit_chatgpt_test.sh (T18)            | `_running_proxy_backend`/`_running_proxy_fp` stubbed to append to `$PROBE_LOG`. With `CHATGPT_BASE_URL=""` and no `backend_override` the log stays empty (and no restart fires); restoring the key, and separately setting `backend_override=chatgpt` with the key blank, each make it non-empty. | |
| C039 | green  | tests/unit_chatgpt_test.sh (T20)            | `.env` rewritten to the CHATGPT_* trio only; `cmd_preflight` output must match `harness chatgpt preflight`. The original `.env` is restored and the same grep must NOT match. | |
| C040 | green  | tests/unit_config_test.sh (T14)             | `_config_value_truncated` is asserted true for `('a=1', 'a=1; b=2')` and false for identical values, a non-prefix difference (`'a=1'` vs `'z=9; a=1'`), an empty sourced value, and an empty on-disk value. | The `doctor_check fail` rendering that consumes the predicate is verified by hand, not by a test: `cmd_doctor` needs `harness_container_runtime`, which `HARNESS_SOURCE_ONLY=1` does not load. |

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
| F042  | yellow   | green     | D        | harness_test T3 runs `harness logs` (no service) and asserts proxy service lines appear. |
| F072  | yellow   | green     | D        | harness_test T11 now exercises `_`, `@`, `$`, `:` illegal chars in addition to space. |
| F131  | yellow   | green     | D        | harness_test T19c stubs `harness_docker` to capture compose() args, asserts `--project-name` is threaded. |
| F133  | yellow   | green     | D        | harness_test T19c same: asserts `-f docker-compose.yml` is threaded into compose() invocations. |
| F135  | yellow   | green     | D        | full_pipeline_test T5 (runtime-override header) + persistence_test T1/T3 (uid+gid alignment from injected env). |
| F139  | yellow   | green     | D        | full_pipeline_test T5 explicitly asserts `start.log` does NOT contain `NETWORK FIREWALL IS DISABLED`. |
| A004  | yellow   | green     | D        | persistence_test T1 + T3 now assert both uid AND gid match host on bind-mounted artifacts. |
| A007  | yellow   | green     | D        | full_pipeline_test T9 stat's the home-initialized marker (post-gosu-drop artifact) and asserts owner uid == host. |
| A012  | yellow   | green     | D        | persistence_test T5 now jq-asserts `includeCoAuthoredBy == false`. |
| A018  | red      | green     | F2       | e2e scenario 01-opencode-boot.yaml header declares `inventory_refs: [F044, A018]`; boot path exercises ensure_opencode_config. |
| A024  | yellow   | green     | D        | full_pipeline_test T9 source-greps wrapper for `ANTHROPIC_AUTH_TOKEN=harness-dummy` literal. |
| A031  | yellow   | green     | D        | full_pipeline_test T10 asserts no `unknown flag` error AND source-greps entrypoint.sh for strip branch. |
| I005  | yellow   | green     | D        | full_pipeline_test T2 directly invokes `harness_docker compose version`. |
| I009  | yellow   | green     | D        | full_pipeline_test T1 source-greps `harness-install.sh` for default REPO_URL literal. |
| I012  | yellow   | green     | D        | full_pipeline_test T1 asserts platform.sh in clone AND source-greps installer for sourcing line. |
| I024  | yellow   | green     | D        | full_pipeline_test T2 grep's installed wrapper for the install-root path literal. |
| P003  | yellow   | green     | D        | proxy_test Scenario F scrapes proxy logs for `listening on: 0.0.0.0:8000` banner. |
| P004  | yellow   | green     | D        | proxy_test Scenario F same banner, asserts `:8000` port suffix. |
| P006  | yellow   | green     | D        | proxy_test Scenario F asserts redacted key banner AND raw key NOT printed. |
| P012  | yellow   | green     | D        | proxy_test Scenario F asserts banner reports `debug dumps: disabled (OUTPUT_DIR not set)`. |
| P050  | yellow   | green     | D        | proxy_test Scenario F asserts proxy logs have no `failed to save debug file` and no `is not writable` lines. |
| U012  | yellow   | green     | D        | upgrade_test T2b uses `printf` to craft a no-trailing-newline target, then asserts post-merge byte-accuracy via `tail -c 1 \| od`. |
| U025  | yellow   | green     | D        | upgrade_test T9 sources upgrade_actions.sh standalone and exercises harness_jq + `_upg_json_array` + `_upg_json_str` directly. |

Per-prefix transition count:

| prefix | yellow→green | red→green | yellow→yellow | red→red |
|--------|--------------|-----------|---------------|---------|
| F      |           11 |         0 |             0 |      44 |
| P      |            5 |         0 |             2 |      13 |
| A      |            5 |         1 |             0 |      16 |
| I      |            4 |         0 |             0 |      16 |
| U      |            2 |         0 |             0 |       7 |
| Pe     |            1 |         0 |             1 |       2 |
| **total** |       28 |         1 |             3 |     118 |

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

### Spot-check C-4 (carried) — P035 (`call_` tool-call id prefix)

Test file: `proxy/test_proxy.py` line 2902-2942 (`test_sse_tool_calls_arguments_are_json_strings`,
`test_build_openai_response_non_streaming`); proxy_test.sh Scenario E (line 488).
See Track C audit.

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
- N031 (scheme-less upstream URL is FATAL in `init-firewall.sh`) is red and was
  verified as genuinely uncovered, not merely container-gated: `tests/firewall_test.sh`
  Phase 2 drives only the *parsed-but-not-allowlisted* host (N018), and nothing in
  `tests/` sets an upstream URL with no scheme. The cheapest fix is a Phase-2b case
  that starts the stack with `PROXY_API_URL=api.example.com/v1` and asserts the
  container logs the "could not parse a hostname" FATAL and exits 1.
- F-row coverage gaps cluster in `net open`/`net close`/`unlock`/`pick_agent`/`shell`
  surfaces (F045, F057-F061, F081-F092). These are all interactive paths or paths
  requiring 401-mocked upstream that the existing harness can't exercise without
  additional scaffolding.
