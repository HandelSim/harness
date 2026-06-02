# Coding Conventions

**Analysis Date:** 2026-06-02

The harness codebase is primarily Bash (the `harness` CLI plus `scripts/`,
`tests/`, container entrypoints) with one Python component (`proxy/proxy.py`,
a Flask OpenAI-compatible translating proxy). There is no ollama component:
the proxy speaks the OpenAI-compatible interface directly and the agent
(opencode) talks to it with no second hop.

## Bash Conventions (primary language)

### Script Boilerplate

Every bash script (CLI, tests, libraries) starts with:

```bash
#!/usr/bin/env bash
set -euo pipefail
```

No exceptions. Tests must fail loudly, not silently
(`architecture/tests.md:110`).

### Function Naming

- **Public command functions:** `cmd_<noun>` or `cmd_<noun>_<verb>` (e.g.,
  `cmd_start`, `cmd_test` at `harness:5030`, `cmd_mcp_install`,
  `cmd_net_allow`)
- **Private helpers:** `_<verb>_<noun>` with underscore prefix (e.g.,
  `_ensure_jq_sidecar`, `_reap_jq_sidecar`, `_upgrade_confirm`,
  `_probe_upstream_auth`, `_parse_prompt_mode_flag`)
- **Lib functions exported for callers:** `harness_<verb>` prefix (e.g.,
  `harness_docker`, `harness_realpath`, `harness_detect_os`,
  `harness_normalize_proxy_env`) — defined in `scripts/lib/platform.sh`
- **Netlib functions:** `netlib_<verb>` prefix (e.g., `netlib_validate_host`,
  `netlib_add_host`) — defined in `scripts/lib/net_helpers.sh`
- **Upgrade-action functions:** `upgrade_<type>` (dispatched by
  `apply_upgrade_actions`) plus `_upg_<helper>` privates
  (`_upg_is_preserved`, `_upg_atomic_mv`, `_upg_json_array`, `_upg_json_str`)
  in `scripts/lib/upgrade_actions.sh`

### Variable Naming

- **Globals:** lowercase, underscore-separated (`install_root`, `clone_dir`,
  `project_name`, `state_root`)
- **Env overrides:** `HARNESS_<UPPER>` for user-facing env vars
  (`HARNESS_INSTALL_ROOT`, `HARNESS_PROJECT_NAME`, `HARNESS_SOURCE_ONLY`,
  `HARNESS_CONTAINER_RUNTIME`, `HARNESS_FIREWALL_DISABLED`)
- **Local variables:** declared with `local` inside functions; lowercase with
  underscores
- **Array variables:** lowercase, with `[@]` expansion (`local -a candidates=()`,
  `local args=(...)`)

### Error Output

All error output goes to stderr. The standard format for user-facing errors
from `harness` (`harness:179`):

```bash
err() {
    echo "harness: $*" >&2
}
```

Test scripts use their own local `fail()` helper:

```bash
fail() {
    echo "[<test-name>] FAIL: $*" >&2
    exit 1
}
```

### Source Guards and Library Loading

Libraries are sourced conditionally with existence guards:

```bash
if [[ -f "$clone_dir/scripts/lib/platform.sh" ]]; then
    # shellcheck disable=SC1091
    source "$clone_dir/scripts/lib/platform.sh"
fi
```

Libraries used by tests are sourced at the top of the test file via
`REPO_ROOT`:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/lib/platform.sh"
```

Net helpers (`scripts/lib/net_helpers.sh`) are lazily loaded via
`load_net_helpers()` to avoid paying their startup cost on commands that never
touch the network.

`scripts/lib/upgrade_actions.sh` self-provides a `harness_jq` shim when sourced
standalone (guarded `if ! declare -F harness_jq`), so the upgrade library works
both inside the wrapper and in isolation — a contract tested directly
(U025, `tests/upgrade_test.sh:537-580`).

### Shellcheck Directives

Inline `# shellcheck disable=SCxxxx` suppressions are used for known-safe
dynamic sources. Shellcheck runs in CI in advisory mode
(`continue-on-error: true`, `.github/workflows/ci.yml:56-58`) with
`severity: warning`.

### Container Runtime Wrapping (enforced)

**All** container invocations must go through `harness_docker` /
`harness_docker_exec` from `scripts/lib/platform.sh` — never raw `docker`.
This is enforced by `scripts/check_runtime_calls.sh`, which CI runs on every
push (the `lint` job).

```bash
# WRONG — breaks podman support, fails check_runtime_calls.sh
docker compose up -d

# CORRECT
harness_docker compose up -d
```

`HARNESS_CONTAINER_RUNTIME=podman` swaps the runtime; `harness_docker` honors
it. The same wrapper threads the host proxy env (all four
`HTTP_PROXY`/`http_proxy`/`HTTPS_PROXY`/`https_proxy` spellings) into
`compose build`/BuildKit but never into running containers.

### Inline Comments

Section separators use `# ---`:

```bash
# --- section name ----------------------------------------------------------
```

Long inline comments explain non-obvious decisions above the code they
describe. Comment blocks above functions explain purpose, arguments, and
caveats — dense prose, not a formal JSDoc style.

### Exit Codes

- `exit 0` — success
- `exit 1` — generic failure (also `return 1` for functions)
- `_probe_upstream_auth` uses a documented tri-state: `0` on HTTP 200, `1` on
  401/403, `2` on connection failure (F093)
- Functions return failure; callers abort under `set -e`, or check `rc`
  explicitly with the `rc=0; cmd || rc=$?` idiom

### Case Statements (dispatch)

Command dispatch uses `case` with aligned `;;` and aligned labels. Subcommands
have one-liner dispatch entries; the catch-all errors to stderr:

```bash
case "$cmd" in
    help)     cmd_help ;;
    start)    cmd_start "$@" ;;
    *)
        err "unknown command '$cmd'"
        exit 1
        ;;
esac
```

### Heredocs

In-script heredocs use `<<'EOF'` (single-quoted, no expansion) for literal
content such as test fixtures and help text. Variable-expanding content uses
`<<EOF` (unquoted).

### Argument Parsing in Functions

Command functions parse their own flags via
`while (( $# > 0 )); do case "$1" in ... esac; shift; done`. Boolean flags set
local integer variables (`local check_only=0`).

### Traps

Tests always register a `cleanup` trap on `EXIT INT TERM`:

```bash
cleanup() {
    [[ -n "${WORK:-}" && -d "${WORK}" ]] && rm -rf "${WORK}"
}
trap cleanup EXIT INT TERM
```

### Arithmetic

Arithmetic tests use `(( ... ))` not `[ ... -eq ... ]`:

```bash
if (( ${#missing[@]} > 0 )); then ...
```

### Quoting

Always quote variable expansions: `"$var"`, `"${var}"`, `"${array[@]}"`. Array
expansions use `"${arr[@]}"`, never `$arr`.

---

## Python Conventions (`proxy/proxy.py`, `proxy/test_proxy.py`)

### Type Annotations

All module-level constants and function signatures carry type hints:

```python
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
MODEL_CONTEXT_LENGTH: int = int(...)            # proxy.py:65
def _normalize_api_base(url: str) -> str:
```

`typing` imports: `Any`, `Dict`, `Iterable`, `List`, `Optional`, `Tuple`.

### Module-Level Constants

Project-managed constants that are NOT user knobs are prefixed with `_` and
carry a block comment explaining why they exist and why they are not env vars
(`proxy/proxy.py`):

```python
_PROMPT_MODE: str = "hybrid"            # proxy.py:127, set in main() before serving
_CHANGE_SYSTEM_TO_USER: bool = True     # proxy.py:137, hardcoded (no longer env-read)
_HYBRID_DETAIL_TOOLS: List[str] = ["task", "skill"]   # proxy.py:151
```

Tests patch these via `patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True)`,
never via env vars (the `_setup_change_system_to_user` /
`_setup_hybrid_detail_tools` env-parse helpers were removed).

### Docstrings

The module docstring lists every env var, its default, and which are REQUIRED
(`proxy/proxy.py:1-40`). Function docstrings are single-line or short
multi-line, describing behavior — not parameter/return tables.

### Section Comments

Sections delimited with:

```python
# ---------------------------------------------------------------------------
# Section Name
# ---------------------------------------------------------------------------
```

### Error Handling

Flask route handlers catch `Exception` broadly, log the traceback, dump to the
`99_Fatal_Error_*` file when `OUTPUT_DIR` is set, and return HTTP 502. Upstream
HTTP error codes are forwarded verbatim with a `print()` to stderr: 401/403
print the unlock URL, 429 prints a rate-limit warning, 5xx prints a warning,
connection failure and non-JSON both surface as 502 (P037-P042).

### Imports

Standard library first, then third-party (`requests`, `flask`). `# noqa: E402`
suppresses the `import proxy` line in `test_proxy.py` because required env vars
must be set before import.

---

## Environment-Variable Conventions

Everything configurable is an env var; nothing operationally variable is
hardcoded. Key conventions:

- **REQUIRED proxy vars** (no default, fail-fast at startup): `PROXY_API_URL`,
  `PROXY_API_KEY`, `DEFAULT_MODEL_NAME` (`proxy/proxy.py` module docstring;
  `require_runtime_config` / `harness preflight` enforce on the CLI side, F105).
- **Legacy aliases** are honored for back-compat: `MODEL_CONTEXT_LENGTH` reads
  first, falling back to `OLLAMA_CONTEXT_LENGTH` when unset
  (`proxy/proxy.py:65-67`). This is the only surviving `OLLAMA_*` knob — a
  compatibility alias, not a live ollama dependency.
- **Prompt mode is NOT a `.env` knob.** `PROXY_PROMPT_MODE` is injected onto
  the proxy container only via `harness start/restart --prompt-mode <mode>`
  (validated to `hybrid`/`user_front`/`passthrough` by
  `_parse_prompt_mode_flag`, ephemeral via `write_runtime_override`, F150). The
  proxy defaults to `hybrid` and warns on any unknown/removed value
  (`proxy/proxy.py:305-314`, P018).
- **CLI overrides** all use the `HARNESS_<UPPER>` namespace
  (`HARNESS_INSTALL_ROOT`, `HARNESS_ALLOWLIST_PATH`,
  `HARNESS_NET_OVERRIDES_PATH`, `HARNESS_REGISTRY_DIR`, `HARNESS_PROJECT_NAME`,
  `HARNESS_SOURCE_ONLY`, `HARNESS_CONTAINER_RUNTIME`, `HARNESS_RUN_SLOW`).
- **Never hardcode secrets, paths, or names that change between environments.**
  Secrets live only in `.env` at the install root (never committed, never
  echoed — the proxy banner redacts `PROXY_API_KEY` to `test...1234` form and
  asserts the raw key is never printed, P006).

---

## Commit & Doc-Update Conventions

### Commit messages

Conventional-commit prefixes are used throughout the history
(`feat(mcp):`, `test(mcp):`, `docs(gsd):`, `test+docs:`). Issue-driven agent
work uses two additional forms (`.claude/references/workflows/implementing.md`):

- **Checkpoint commits:** `WIP #<N>: <what>` — pushed as plain fast-forwards as
  work proceeds; may be incomplete or red.
- **Final commit:** `Fix #<N>: <summary>` with the trailer
  `Co-authored-by: HandelSim <HandelSim@users.noreply.github.com>`.

### Doc-update-same-commit rule

When a change alters behavior covered by an architecture doc, the doc under
`architecture/` is updated **in the same commit** as the code change (CLAUDE.md,
"Doc-update rule"). The architecture router table in `CLAUDE.md` is the lookup
for which doc covers which subject:

| Touching… | Update |
|-----------|--------|
| `harness` CLI, subcommands, agent launch, doctor | `architecture/harness-cli.md` |
| `proxy/proxy.py`, prompt modes, tool extraction, SSE | `architecture/proxy.md` |
| upstream API contract, key lifecycle, schema | `architecture/upstream-api.md` |
| `docker-compose.yml`, `agents/`, `firewall/`, entrypoints | `architecture/containers.md` |
| `mcp-registry/`, `state/mcp/`, MCP lifecycle | `architecture/mcp.md` |
| `harness-install.sh`, upgrade manifest/actions | `architecture/install-and-upgrade.md` |
| anything under `tests/` (beyond one fixture/assertion) | `architecture/tests.md` |

The inventory (`tests/INVENTORY.md`) and coverage map (`tests/COVERAGE.md`)
must be updated when behaviors are added or removed. Stable IDs (`F###`,
`P###`, `A###`, `M###`, `N###`, `U###`, `Pe###`, `I###`) are never reused or
renumbered. The `O###` prefix (ollama entrypoint) is retired — zero rows.

---

## Local Testing Discipline (agent-facing)

Per `CLAUDE.md` ("Local testing during issue work"), issue-handling agents
verify **docker-free only** and leave docker-based suites to CI:

- Permitted locally: `bash -n` on touched scripts, `scripts/check_runtime_calls.sh`,
  advisory `shellcheck`, and the docker-free unit slice (`harness test unit`,
  or a single `unit_*_test.sh`).
- Forbidden locally from an issue agent: bare `harness test`, any docker
  section (`proxy`, `harness`, `persistence`, `mcp`, `firewall`,
  `scheme_contract`), `--slow`/`HARNESS_RUN_SLOW=1`, `integration_test.sh`,
  `full_pipeline_test.sh`, and any `harness benchmark` target.
- **Commit and push a checkpoint before running any test.** A hang during a
  docker build is a known failure mode that has lost work.

---

## Naming Patterns Summary

| Context | Pattern | Example |
|---------|---------|---------|
| Shell libraries | `<subject>_<verb>.sh` | `upgrade_actions.sh`, `net_helpers.sh` |
| Test scripts | `<name>_test.sh` | `harness_test.sh`, `upgrade_test.sh` |
| Unit tests | `unit_<subject>_test.sh` | `unit_workdir_test.sh` |
| Lib functions | `harness_<verb>` | `harness_docker`, `harness_realpath` |
| Netlib functions | `netlib_<verb>` | `netlib_validate_host` |
| Upgrade actions | `upgrade_<type>` / `_upg_<helper>` | `upgrade_envfile_merge`, `_upg_atomic_mv` |
| CLI commands | `cmd_<noun>` | `cmd_start`, `cmd_test` |
| Private helpers | `_<verb>` or `_<verb>_<noun>` | `_reap_jq_sidecar`, `_probe_upstream_auth` |
| Env overrides | `HARNESS_<UPPER>` | `HARNESS_INSTALL_ROOT` |
| Python no-knob constants | `_UPPER_SNAKE` | `_CHANGE_SYSTEM_TO_USER`, `_HYBRID_DETAIL_TOOLS` |
| Inventory IDs | `<prefix>###` | `F001`, `P033`, `M033` |

---

*Convention analysis: 2026-06-02*
