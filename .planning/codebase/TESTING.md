# Testing Patterns

**Analysis Date:** 2026-06-02

## Test Framework

**Runner:**
- Bash test scripts executed directly via `bash tests/<name>_test.sh`
- Python unit tests via `python -m unittest` (proxy only)
- `harness test [section]` is the unified runner entry point (implemented in `cmd_test` at `harness:4660`)

**Assertion Library:**
- Bash: `[[ ... ]]` conditionals, `grep -q`, `grep -E`, inline `fail()` helpers
- Python: `unittest.TestCase` methods (`assertEqual`, `assertIn`, `assertTrue`, `assertRaises`)
- No external assertion frameworks (no bats, no pytest)

**Run Commands:**
```bash
harness test                        # all CI-runnable tests (*_test.sh glob)
harness test unit                   # docker-free: upgrade_test.sh + unit_*_test.sh
harness test proxy                  # tests/proxy_test.sh (docker required)
harness test --pattern 'mcp*'       # explicit glob under tests/
harness test integration --slow     # HARNESS_RUN_SLOW=1 (docker required)
bash tests/<name>_test.sh           # run one script directly
bash ./harness test unit            # equivalent to `harness test unit` without install
```

## Test File Organization

**Location:** All test scripts live under `tests/`. Co-located Python unit test in `proxy/test_proxy.py`.

**Naming:**
- `tests/<name>_test.sh` — top-level bash test scripts (one per area)
- `tests/unit_<name>_test.sh` — docker-free unit tests, auto-discovered by `harness test unit`
- `proxy/test_proxy.py` — Python unit tests for pure-Python functions in `proxy/proxy.py`

**Directory layout:**
```
tests/
├── INVENTORY.md            # Stable-ID flat list of all testable behaviors
├── COVERAGE.md             # Per-ID coverage map (green/yellow/red + evidence)
├── README.md               # Quick start + links
├── lib/
│   └── test_helpers.sh     # Shared bash toolkit (require_docker, fixtures, cleanup)
├── fixtures/
│   └── responses/          # Mock-upstream JSON fixture files (NN_slug.json)
├── mock_upstream.py        # Flask LLM API stand-in
├── harness_test.sh         # CLI surface, doctor, preflight, net, upgrade (docker)
├── proxy_test.sh           # Proxy round-trip black-box + delegates to test_proxy.py
├── persistence_test.sh     # State persistence across agent runs (docker)
├── mcp_test.sh             # MCP lifecycle install/enable/disable/up/down (docker)
├── firewall_test.sh        # Egress firewall guardrails (docker)
├── full_pipeline_test.sh   # End-to-end install → start → agent → down (docker)
├── integration_test.sh     # Serena MCP, --mount paths (docker, HARNESS_RUN_SLOW=1)
├── scheme_contract_test.sh # Per-prompt-mode proxy contract (docker)
├── upgrade_test.sh         # Upgrade action library (NO docker)
├── unit_workdir_test.sh    # Cross-platform path helpers (NO docker)
├── unit_platform_timer_test.sh  # Timer/timeout helpers (NO docker)
├── benchmarks/             # Harbor-based agent benchmarks (NEVER run in CI)
└── podman_smoke_test.sh    # Manual podman runtime smoke (NOT in CI)
proxy/
└── test_proxy.py           # Pure-Python unittest for proxy.py helpers (1017 lines)
```

## Docker Requirements Per Test Script

| Script | Docker Required | CI Job | Slow Gate |
|--------|----------------|--------|-----------|
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
| `podman_smoke_test.sh` | Yes (podman) | Not in CI | Manual only |

## Test Structure

### Bash Test Scripts

**Suite organization:** Sequential numbered sections `T0`, `T1`, ... identified by echo headers:

```bash
echo "[harness-test] T1: harness start"
# ... setup, assertions ...
echo "[harness-test] T1 OK"
```

The shared `test_section` helper from `tests/lib/test_helpers.sh` produces colored section banners:

```bash
test_section "T3: harness logs ollama (timeout 5s)"
```

**Setup:**
```bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/lib/platform.sh"   # or tests/lib/test_helpers.sh

TEST_ROOT="$(mktemp -d -t harness-<name>.XXXXXX)"
cleanup() {
    # docker compose down + rm -rf $TEST_ROOT
}
trap cleanup EXIT INT TERM
```

**Assertion pattern:**
```bash
out=$(some_command 2>&1)
rc=0
some_command || rc=$?
[[ $rc -ne 0 ]] || { echo "FAIL: expected non-zero" >&2; exit 1; }
grep -q 'expected substring' <<<"$out" || { echo "FAIL: not found" >&2; exit 1; }
```

Real assertions use `[[ "$x" == "$y" ]]`, `grep -q '<specific>'`, `grep -qE '<pattern>'` — never `[[ -n "$x" ]]` against generic output.

### Python Unit Tests (`proxy/test_proxy.py`)

**Test class organization:** One `TestCase` class per logical feature area:

```python
class TestFormatTools(unittest.TestCase): ...
class TestExtractToolCall(unittest.TestCase): ...
class TestExtractToolCallScanner(unittest.TestCase): ...
class TestTranslateHistory(unittest.TestCase): ...
class TestMakeChunk(unittest.TestCase): ...
class TestPromptInjectionModes(unittest.TestCase): ...
class TestChangeSystemToUser(unittest.TestCase): ...
class TestUsageOverride(unittest.TestCase): ...
class TestToolResultDelimiting(unittest.TestCase): ...
class TestHybridDetailTools(unittest.TestCase): ...
class TestConfigHelpers(unittest.TestCase): ...
class TestModelPassthrough(unittest.TestCase): ...
class TestHostOsSetup(unittest.TestCase): ...
```

**Setup for module import:**
```python
# Set required env vars BEFORE importing proxy so module-level init doesn't fail
os.environ.setdefault("PROXY_API_URL", "http://example.invalid")
os.environ.setdefault("PROXY_API_KEY", "test-key-1234")
os.environ.setdefault("DEFAULT_MODEL_NAME", "test-model")
import proxy  # noqa: E402
```

**Patching project-managed constants** (not env vars):
```python
with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
    result = proxy.translate_history_and_apply_prompt(...)
```

## Mocking

### Bash: Function-Level Stubbing

Tests source the harness script with `HARNESS_SOURCE_ONLY=1` and override functions in the same subshell:

```bash
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    _probe_upstream_auth() { return 1; }   # stub to return locked
    require_docker() { :; }               # no-op
    ensure_services_up() { echo "SENTINEL"; }
    run_agent opencode 2>&1
) || rc=$?
```

Subshells are used (`(...)`) so stubs don't leak between test cases.

**Stubbing `harness_docker`** to capture arguments:
```bash
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    recorded_args=()
    harness_docker() { recorded_args+=("$@"); }
    export -f harness_docker
    compose ps --sentinel-token
)
grep -q 'sentinel-token' <<<"${recorded_args[*]}"
```

### Python: `unittest.mock.patch`

```python
from unittest.mock import patch

with patch.object(proxy, "_PROMPT_MODE", "user_front"):
    result = proxy.translate_history_and_apply_prompt(...)

with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
    ...
```

### Docker-Level: Mock Upstream

Every docker-based test wires the proxy to `tests/mock_upstream.py` instead of a real LLM API. Two modes:

**Legacy (single-response):**
```bash
# Set in .env file for the test stack
MOCK_SCENARIO=text   # or: tool
```

**Fixture dispatch (multi-response):**
```bash
MOCK_FIXTURES_DIR=/fixtures
# Mount tests/fixtures/responses/ at /fixtures
# Fixture files: NN_short_slug.json, matched on last user message regex
# 99_default.json is the catch-all
```

The `test_generate_mockupstream_override` helper in `tests/lib/test_helpers.sh` writes a docker compose override that adds the `mockupstream` service to the `harness-net` network.

For `integration_test.sh` (which uses real `harness start`), `test_start_mockupstream` attaches the mock container to the existing compose network via `docker run --network-alias mockupstream`.

## Fixtures and Factories

**Test env files:** Generated by `test_generate_env <path> [extra_kv...]` from `tests/lib/test_helpers.sh`:
```bash
ENV_FILE=$(mktemp -t harness-foo.XXXXXX.env)
test_generate_env "$ENV_FILE" "MOCK_SCENARIO=tool" "PUBLISH_OLLAMA_PORT=11434"
```

**Allowlist files:** Generated by `test_generate_allowlist <path> [extra_host...]`:
```bash
test_generate_allowlist "$ALLOWLIST_FILE" api.anthropic.com
```

**Compose override files:** Generated by `test_generate_mockupstream_override <path>` for adding the mock upstream service.

**Mock response fixtures:** `tests/fixtures/responses/NN_slug.json` files with:
- `match` — regex matched against the last user message (tool scaffolding stripped first)
- `response` — the fixture response body

**Test workdirs:** Always under a `$(mktemp -d -t harness-<name>.XXXXXX)` tmpdir, cleaned by `trap cleanup EXIT`.

**Fake install roots:** `harness_test.sh` creates `$TEST_ROOT` with a symlink `$TEST_ROOT/harness -> $REPO_ROOT`, then exports `HARNESS_INSTALL_ROOT=$TEST_ROOT` and `HARNESS_PROJECT_NAME=harness-mgmt-test` to isolate from any real harness instance.

**Fake MCP registries:** `mcp_test.sh` creates `FAKE_REGISTRY` with synthetic `test_mcp/` entries and exports `HARNESS_REGISTRY_DIR=$FAKE_REGISTRY`.

## Coverage

**Tracking mechanism:** `tests/COVERAGE.md` — a per-ID coverage table (green/yellow/red) with quoted assertion evidence and file:line references. Updated manually when test behavior changes.

**Current stats (from COVERAGE.md):**

| Status | Count | Percent |
|--------|-------|---------|
| green  | 252   | 65.6%   |
| yellow | 5     | 1.3%    |
| red    | 127   | 33.1%   |

**Requirements:** No enforced minimum coverage threshold in CI.

**Run coverage view:**
No automated coverage tool. Coverage is tracked by audit in `tests/COVERAGE.md`.

## CI Jobs

All jobs defined in `.github/workflows/ci.yml`. Runs on push/PR to `dev` and `main`.

| Job | What runs | Timeout | Docker |
|-----|-----------|---------|--------|
| `lint` | `bash -n` all shell scripts; `scripts/check_runtime_calls.sh`; advisory shellcheck | 10 min | No |
| `unit` | `bash ./harness test unit` (upgrade_test.sh + unit_*_test.sh) | 10 min | No |
| `docker-tests` | Matrix: harness_test, proxy_test, persistence_test, mcp_test, firewall_test | 25 min each | Yes |
| `pipeline` | `tests/full_pipeline_test.sh` | 30 min | Yes |
| `integration` | `HARNESS_RUN_SLOW=1 tests/integration_test.sh` | 45 min | Yes |
| `scheme_contract` | `tests/scheme_contract_test.sh` | 15 min | Yes |

Docker jobs relocate the Docker data-root to `/mnt` (66 GB free) before building to prevent disk exhaustion from buildx layer exports.

Buildx layers are cached per-job using `actions/cache@v4` keyed on Dockerfile hashes.

## Adding a New Test

1. Drop `tests/<name>_test.sh`; glob-discovery by `cmd_test` picks it up automatically (no wrapper edits needed)
2. Source `tests/lib/test_helpers.sh` and call `require_docker` at the top
3. Use `test_section "..."` before each block; `test_cleanup` on EXIT
4. Assert on structured output, not just exit codes: `grep -q '<specific>'` not `[[ -n "$output" ]]`
5. Wire mock upstream via `test_generate_mockupstream_override` — never hit a real LLM API
6. Gate slow tests with `[[ "${HARNESS_RUN_SLOW:-0}" == "1" ]] || exit 0`
7. Add the script name to the `matrix.test` list in `.github/workflows/ci.yml` (by hand — glob-based discovery is intentionally avoided in CI so adding a test always triggers explicit review)
8. Add behaviors to `tests/INVENTORY.md` with stable IDs; add coverage rows to `tests/COVERAGE.md`

## Common Patterns

**Waiting for healthy containers:**
```bash
test_wait_for_healthy "$PROJECT" proxy ollama 90
```

**Cleanup of test stacks:**
```bash
test_cleanup "$PROJECT" "$ENV_FILE" "$OVERRIDE_FILE"
```

**Inline assertion with fail message:**
```bash
grep -q 'expected pattern' <<<"$output" \
    || { echo "[test] FAIL: expected pattern not found in: $output" >&2; exit 1; }
```

**Testing process exit codes:**
```bash
rc=0
some_command 2>/dev/null || rc=$?
[[ $rc -eq 1 ]] || { echo "FAIL: expected rc=1, got $rc" >&2; exit 1; }
```

**Python async testing:** Not used; `proxy.py` is synchronous Flask.

**Python error testing:**
```python
with self.assertRaises(SystemExit):
    proxy.some_function_that_exits()
```

---

*Testing analysis: 2026-06-02*
