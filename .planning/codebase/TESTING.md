# Testing Patterns

**Analysis Date:** 2026-06-02

The test suite targets the OpenAI-only topology: the proxy
(`proxy/proxy.py`) serves an OpenAI-compatible interface that opencode talks
to directly, with no ollama hop (`tests/proxy_test.sh:7`). Tests assert
`call_`-prefixed tool-call ids, `chat.completion` / `chat.completion.chunk`
object shapes, `data: [DONE]` stream termination, and the
`04_OpenAI_Response_*` / `04_OpenAI_SSE_Response_*` debug-dump prefixes. The
former ollama entrypoint inventory (`O###`) is retired with zero rows; the
only `ollama` strings remaining in `tests/` are retirement comments and the
`OLLAMA_CONTEXT_LENGTH` legacy-alias mention.

## Test Framework

**Runner:**
- Bash test scripts executed directly via `bash tests/<name>_test.sh`
- Python unit tests via `python -m unittest` (proxy only)
- `harness test [section]` is the unified runner entry point (`cmd_test` at
  `harness:5030`). It globs `tests/*_test.sh`, so a new
  `tests/<name>_test.sh` is auto-discovered with no wrapper edit
  (`architecture/tests.md:32-34`).

**Assertion style:**
- Bash: `[[ ... ]]` conditionals, `grep -q`, `grep -qE`, inline `fail()` /
  `{ echo FAIL >&2; exit 1; }` helpers
- Python: `unittest.TestCase` methods (`assertEqual`, `assertIn`,
  `assertTrue`, `assertRaises`)
- No external assertion frameworks (no bats, no pytest)

**Run Commands:**
```bash
harness test                        # all CI-runnable tests (*_test.sh glob)
harness test unit                   # docker-free: upgrade_test.sh + unit_*_test.sh
harness test proxy                  # tests/proxy*_test.sh (docker required)
harness test --pattern 'mcp*'       # explicit glob under tests/ (overrides section)
harness test integration --slow     # sets HARNESS_RUN_SLOW=1 (docker required)
bash tests/<name>_test.sh           # run one script directly
bash ./harness test unit            # equivalent without an installed wrapper
```

## Test File Organization

**Location:** All bash test scripts live under `tests/`. The one Python unit
test is co-located with its target: `proxy/test_proxy.py`.

**Naming:**
- `tests/<name>_test.sh` — top-level bash scripts (one per area)
- `tests/unit_<name>_test.sh` — docker-free unit tests, run by
  `harness test unit`
- `proxy/test_proxy.py` — Python unit tests for the pure-Python functions in
  `proxy/proxy.py`

**Directory layout:**
```
tests/
├── INVENTORY.md            # Stable-ID flat list of all testable behaviors
├── COVERAGE.md             # Per-ID coverage map (green/yellow/red + evidence)
├── README.md               # Quick start + links to architecture/tests.md
├── lib/
│   └── test_helpers.sh     # Shared bash toolkit (311 lines): require_docker,
│                           #   test_section, test_generate_env, test_cleanup,
│                           #   test_wait_for_healthy, mockupstream helpers
├── fixtures/
│   ├── responses/          # Mock-upstream JSON fixtures (NN_slug.json)
│   └── test-project/       # Calculator package used by integration_test.sh
├── mock_upstream.py        # Flask LLM-API stand-in (fixture dispatch + legacy)
├── harness_test.sh         # CLI surface, doctor, preflight, net, upgrade flags (1452 lines)
├── proxy_test.sh           # Proxy round-trip black-box + delegates to test_proxy.py (514 lines)
├── persistence_test.sh     # State persistence across agent runs
├── mcp_test.sh             # MCP lifecycle + register validation gate (1103 lines)
├── firewall_test.sh        # Egress firewall guardrails (324 lines)
├── full_pipeline_test.sh   # End-to-end install → start → agent → down
├── integration_test.sh     # Serena MCP, --mount paths (900 lines, slow-gated)
├── scheme_contract_test.sh # Per-prompt-mode proxy contract (456 lines)
├── upgrade_test.sh         # Upgrade action library (586 lines, NO docker)
├── unit_workdir_test.sh    # Cross-platform path helpers (NO docker)
├── unit_platform_timer_test.sh  # Timer/timeout helpers (NO docker)
├── benchmarks/             # Harbor agent benchmarks (NEVER run in CI)
└── podman_smoke_test.sh    # Manual podman runtime smoke (238 lines, NOT in CI)
proxy/
└── test_proxy.py           # Pure-Python unittest for proxy.py helpers (1017 lines)
```

## Sections / Tiers

`harness test [SECTION]` selects a group (`architecture/tests.md:36-49`):

| Section | What runs | Docker |
|---------|-----------|--------|
| `all` (default) | every `tests/*_test.sh` | mixed |
| `unit` | `upgrade_test.sh` + every `unit_*_test.sh` (glob-discovered) | **No** |
| `integration` | docker-based tests; `--slow` adds `integration_test.sh` | Yes |
| `<prefix>` | `tests/<prefix>*_test.sh` (e.g. `proxy`, `mcp`) | per script |

### Docker requirement per script

| Script | Docker | CI job | Slow gate |
|--------|--------|--------|-----------|
| `upgrade_test.sh` | No | `unit` | No |
| `unit_workdir_test.sh` | No | `unit` | No |
| `unit_platform_timer_test.sh` | No | `unit` | No |
| `harness_test.sh` | Yes | `docker-tests` matrix | No |
| `proxy_test.sh` | Yes | `docker-tests` matrix | No |
| `persistence_test.sh` | Yes | `docker-tests` matrix | No |
| `mcp_test.sh` | Yes | `docker-tests` matrix | No |
| `firewall_test.sh` | Yes | `docker-tests` matrix | No |
| `full_pipeline_test.sh` | Yes | `pipeline` | No |
| `integration_test.sh` | Yes | `integration` | `HARNESS_RUN_SLOW=1` |
| `scheme_contract_test.sh` | Yes | `scheme_contract` | No |
| `podman_smoke_test.sh` | Yes (podman) | not in CI | manual only |

The docker-free tier (`unit`) is the only thing issue-handling agents run
locally; every docker-based section is left to CI (CLAUDE.md, "Local testing
during issue work").

## Test Structure

### Bash Test Scripts

**Suite organization:** Sequential numbered sections `T0`, `T1`, ...
(`mcp_test.sh` uses `TR1`–`TR10` for the `register` block). Sections are
announced by the shared `test_section` helper from
`tests/lib/test_helpers.sh`:

```bash
test_section "T3: harness logs (timeout 5s)"
```

**Setup:**
```bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/lib/platform.sh"   # or tests/lib/test_helpers.sh

TEST_ROOT="$(mktemp -d -t harness-<name>.XXXXXX)"
cleanup() { : ; }     # docker compose down + rm -rf $TEST_ROOT
trap cleanup EXIT INT TERM
```

**Assertion pattern** (assert on structure, not exit-code-only or non-empty):
```bash
out=$(some_command 2>&1)
rc=0; some_command || rc=$?
[[ $rc -ne 0 ]] || { echo "FAIL: expected non-zero" >&2; exit 1; }
grep -q 'expected substring' <<<"$out" || { echo "FAIL: not found" >&2; exit 1; }
```

Real assertions use `[[ "$x" == "$y" ]]`, `grep -q '<specific>'`,
`grep -qE '<pattern>'`. Always-pass forms like `[[ -n "$x" ]]` against any
non-empty output are explicitly disallowed
(`architecture/tests.md:119-122`).

### Python Unit Tests (`proxy/test_proxy.py`)

**One `TestCase` per logical feature area:** `TestFormatTools`,
`TestExtractToolCall`, `TestExtractToolCallScanner`, `TestTranslateHistory`,
`TestMakeChunk`, `TestPromptInjectionModes`, `TestChangeSystemToUser`,
`TestUsageOverride`, `TestToolResultDelimiting`, `TestHybridDetailTools`,
`TestConfigHelpers`, `TestModelPassthrough`, `TestHostOsSetup`.

**Module import requires env first:**
```python
os.environ.setdefault("PROXY_API_URL", "http://example.invalid")
os.environ.setdefault("PROXY_API_KEY", "test-key-1234")
os.environ.setdefault("DEFAULT_MODEL_NAME", "test-model")
import proxy  # noqa: E402
```

**Patch project-managed constants, not env vars:**
```python
with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
    result = proxy.translate_history_and_apply_prompt(...)
with patch.object(proxy, "_PROMPT_MODE", "user_front"):
    ...
```

`proxy_test.sh` delegates to this file via `python -m unittest` after its own
black-box round-trip scenarios.

## Mocking

### Bash: function-level stubbing in subshells

Tests source the wrapper with `HARNESS_SOURCE_ONLY=1` and override functions in
the same subshell so stubs never leak between cases:

```bash
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    _probe_upstream_auth() { return 1; }     # stub: locked key
    require_docker() { :; }                   # no-op
    ensure_services_up() { echo "SENTINEL"; }
    run_agent opencode 2>&1
) || rc=$?
```

Capturing `harness_docker` args (used for the compose-threading tests, F131/F133):
```bash
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    recorded_args=()
    harness_docker() { recorded_args+=("$@"); }
    compose ps --sentinel-token-d19c
    grep -q 'sentinel-token-d19c' <<<"${recorded_args[*]}"
)
```

### Python: `unittest.mock.patch`

`patch.object(proxy, "_PROMPT_MODE", ...)` and
`patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", ...)` (see above).

### Docker-level: the mock upstream

Every docker-based test wires the proxy to `tests/mock_upstream.py` instead of
a real LLM API (`architecture/tests.md:126-128`). Two dispatch modes, fixture
taking precedence:

**Fixture dispatch (preferred, multi-response):** set
`MOCK_FIXTURES_DIR=/fixtures` and mount `tests/fixtures/responses/`. The mock
loads every `*.json` lexicographically and matches the latest user message
against each fixture's `match` regex; first match wins; `99_default.json` is
the catch-all (`tests/mock_upstream.py:8-30`,
`tests/fixtures/responses/README.md`). The proxy's cooperative-prompt
scaffolding (tool-schema dump) is stripped before matching, so a fixture regex
only ever sees the user's actual request or a tool result.

**Legacy `MOCK_SCENARIO` (fallback, single-response):**
`MOCK_SCENARIO=text|tool` picks one canned response when no fixture matches or
`MOCK_FIXTURES_DIR` is unset (`tests/mock_upstream.py:32-57`). Used by
`proxy_test.sh` and `firewall_test.sh` where one response is enough.

The `test_generate_mockupstream_override` helper writes a compose override that
attaches the `mockupstream` service to `harness-net`. For
`integration_test.sh` (real `harness start`), the mock is attached via a
`--network-alias mockupstream` run.

## Fixtures and Factories

- **Env files:** `test_generate_env <path> [extra_kv...]`
  (`tests/lib/test_helpers.sh`).
- **Allowlist files:** `test_generate_allowlist <path> [extra_host...]`.
- **Compose overrides:** `test_generate_mockupstream_override <path>`.
- **Mock response fixtures:** `tests/fixtures/responses/NN_short_slug.json`
  with `match` (regex on the stripped last user message) and `response`
  (`tests/fixtures/responses/README.md`).
- **Test workdirs:** always `$(mktemp -d -t harness-<name>.XXXXXX)`, removed by
  `trap cleanup EXIT`.
- **Fake install roots:** `harness_test.sh` exports
  `HARNESS_INSTALL_ROOT=$TEST_ROOT` + `HARNESS_PROJECT_NAME=...` to isolate
  from any real instance, and sources the wrapper under `HARNESS_SOURCE_ONLY=1`.
- **Fake MCP registries:** `mcp_test.sh` builds a synthetic `FAKE_REGISTRY` and
  exports `HARNESS_REGISTRY_DIR=$FAKE_REGISTRY`.

## Inventory & Coverage (stable-ID system)

`tests/INVENTORY.md` enumerates every atomic testable behavior with a stable,
never-reused ID grouped by prefix:

| Prefix | Domain |
|--------|--------|
| `F###` | CLI commands/flags (the `harness` wrapper) |
| `P###` | Proxy translation (`proxy/proxy.py`) |
| `A###` | Agent entrypoint (`agents/entrypoint.sh`) |
| `M###` | MCP lifecycle (`harness mcp …`, compose, registry) |
| `N###` | Firewall guardrails (`firewall/init-firewall.sh`, net overrides) |
| `U###` | Upgrade actions (`scripts/lib/upgrade_actions.sh`) |
| `Pe###` | Persistence (what survives lifecycle/upgrade) |
| `I###` | Installer (`harness-install.sh`) + `platform.sh` primitives |
| `O###` | **Retired** — ollama entrypoint gone; zero rows |

`tests/COVERAGE.md` maps each ID to its test file/line with a status:

- **green** — a test exists AND its assertions check the claimed behavior
  (evidence quotes a real assertion at a real `file:line`).
- **yellow** — the code path runs but only a weak check (return code / non-empty /
  "no crash"); the weak assertion is quoted.
- **red** — no test exercises the behavior.

**Current counts.** `tests/INVENTORY.md:14` states 372 inventory IDs
(F=146, P=58, A=24, M=23, N=30, U=29, Pe=16, O=0, I=44). The
`tests/COVERAGE.md:58-65` summary table totals 366 rows: **243 green
(66.3%), 5 yellow (1.4%), 118 red (32.2%)**. (The two figures do not fully
reconcile — the audit's own per-prefix table at `COVERAGE.md:69-79` lists
F=146 / P=56 / A=22 / M=23 / N=30 / U=29 / Pe=16 / O=0 / I=44 = 366. Treat
the COVERAGE.md status table as the live coverage figure and the small
inventory-vs-audit delta as a known accounting drift, not new untested
behavior.) The five yellows are F102, F142, P008, Pe006, I044 —
indirect-evidence rows.

No enforced coverage threshold in CI; coverage is tracked by manual audit, not
an automated tool. Regression-detection spot-checks (inject a deliberate bug,
confirm RED, revert, confirm GREEN) are logged at `COVERAGE.md:616-700`.

## Current Coverage Gaps

From `COVERAGE.md:704-721`:

- **Debug-dump infrastructure (P043-P050) is entirely red.** Test scaffolding
  sets `OUTPUT_DIR=` empty to bypass it; no test points `OUTPUT_DIR` at a
  tmpdir and asserts the `01_Inbound_Request_*` … `04_OpenAI_Response_*` /
  `04_OpenAI_SSE_Response_*` / `99_Fatal_Error_*` prefixes appear. `Pe006`
  (`state/output/`) is correspondingly yellow.
- **Upstream HTTP error forwarding (P037-P042) is entirely red.**
  `mock_upstream.py` returns only 200; asserting 401/403/429/5xx/502 behavior
  needs an error-injecting mode.
- **Firewall/iptables rule state (N002-N011, N013-N016, N019-N028) is mostly
  red.** No test reads back actual `iptables`/`ipset` state from a clean
  firewall init; only bypass (N001, N017) and a focused negative (N018) are
  covered.
- **Interactive / auth-gated CLI paths are red** (F045 `shell`, F057-F061
  `pick_agent`/`stop`, F081-F092 `net open`/`net close`/`unlock`). These need
  TTY mocking or a 401-mocked upstream the current harness can't yet drive.
- **Several agent-config assertions are red** (A019/A020/A021/A022/A028/A029)
  — opencode provider/yolo/mcp-merge details are exercised only at the
  boot-smoke level (A018, A035, A036), not field-by-field.

## CI Matrix

All jobs in `.github/workflows/ci.yml`; run on push/PR to `dev` and `main`:

| Job | What runs | Docker |
|-----|-----------|--------|
| `lint` | `bash -n` over all shell scripts; `scripts/check_runtime_calls.sh`; advisory `shellcheck` (`continue-on-error`) | No |
| `unit` | `harness test unit` (`upgrade_test.sh` + `unit_*_test.sh`) | No |
| `docker-tests` | matrix: `harness_test`, `proxy_test`, `persistence_test`, `mcp_test`, `firewall_test` (`.github/workflows/ci.yml:90-95`) | Yes |
| `pipeline` | `tests/full_pipeline_test.sh` (T9 boot-smoke also asserts A018) | Yes |
| `integration` | `HARNESS_RUN_SLOW=1 tests/integration_test.sh` | Yes |
| `scheme_contract` | `tests/scheme_contract_test.sh` (per-`PROXY_PROMPT_MODE` body capture) | Yes |

Benchmarks (`harness benchmark …`) NEVER run in CI — they need an upstream API
key, hours of wall-clock, and large disk (`tests/benchmarks/README.md`).
`podman_smoke_test.sh` is not in CI (no podman on `ubuntu-latest`); run it
manually on Linux when touching the runtime wrapper.

## Adding a New Test

1. Drop `tests/<name>_test.sh`; `cmd_test` glob-discovery picks it up — no
   wrapper edit. Add the script to the `docker-tests` matrix in
   `.github/workflows/ci.yml` by hand if it needs docker (CI uses an explicit
   matrix so a new docker test triggers review).
2. `#!/usr/bin/env bash` + `set -euo pipefail`; source
   `tests/lib/test_helpers.sh`; call `require_docker` at the top for
   docker-based tests.
3. `test_section "..."` before each block; register `test_cleanup` on
   `EXIT`/`ERR`.
4. Assert on structure (`grep -q '<specific>'`), never `[[ -n "$output" ]]`.
5. Wire mock upstream via `test_generate_mockupstream_override` (or a fixture
   under `tests/fixtures/responses/`) — never hit a real LLM API.
6. Gate slow tests (> ~60 s or heavy image) behind
   `[[ "${HARNESS_RUN_SLOW:-0}" == "1" ]] || exit 0`.
7. Add behaviors to `tests/INVENTORY.md` with new stable IDs; add coverage rows
   to `tests/COVERAGE.md` with quoted assertion evidence.

## Common Patterns

**Waiting for healthy containers** (the proxy plus `mockupstream`, not
ollama):
```bash
test_wait_for_healthy "$PROJECT" mockupstream proxy 120
```

**Cleanup of test stacks:**
```bash
test_cleanup "$PROJECT" "$ENV_FILE" "$OVERRIDE_FILE"
```

**Process exit-code assertion:**
```bash
rc=0; some_command 2>/dev/null || rc=$?
[[ $rc -eq 1 ]] || { echo "FAIL: expected rc=1, got $rc" >&2; exit 1; }
```

**Python error testing:**
```python
with self.assertRaises(SystemExit):
    proxy.some_function_that_exits()
```

Python async testing is not used; `proxy.py` is synchronous Flask.

---

*Testing analysis: 2026-06-02*
