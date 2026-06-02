# Coding Conventions

**Analysis Date:** 2026-06-02

## Bash Conventions (primary language)

### Script Boilerplate

Every bash script (CLI, tests, libraries) starts with:

```bash
#!/usr/bin/env bash
set -euo pipefail
```

No exceptions. Tests must fail loudly, not silently.

### Function Naming

- **Public command functions:** `cmd_<noun>` or `cmd_<noun>_<verb>` (e.g., `cmd_start`, `cmd_mcp_install`, `cmd_net_allow`)
- **Private helpers:** `_<verb>_<noun>` with underscore prefix (e.g., `_ensure_jq_sidecar`, `_reap_jq_sidecar`, `_upgrade_confirm`, `_probe_upstream_auth`)
- **Lib functions exported for callers:** `harness_<verb>` prefix (e.g., `harness_docker`, `harness_realpath`, `harness_detect_os`, `harness_normalize_proxy_env`)
- **Netlib functions:** `netlib_<verb>` prefix (e.g., `netlib_validate_host`, `netlib_add_host`)

### Variable Naming

- **Globals:** lowercase, underscore-separated (`install_root`, `clone_dir`, `project_name`, `state_root`)
- **Env overrides:** `HARNESS_<UPPER>` for user-facing env vars (`HARNESS_INSTALL_ROOT`, `HARNESS_PROJECT_NAME`, `HARNESS_SOURCE_ONLY`)
- **Local variables:** declared with `local` inside functions; lowercase with underscores
- **Array variables:** lowercase, appended with `[@]` syntax (`local -a candidates=()`, `local args=(...)`)

### Error Output

All error output goes to stderr. The standard format for user-facing errors from `harness`:

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

Libraries used by tests are sourced at the top of the test file via `REPO_ROOT`:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/lib/platform.sh"
```

Net helpers (`scripts/lib/net_helpers.sh`) are lazily loaded via `load_net_helpers()` to avoid paying their startup cost on commands that never touch the network.

### Shellcheck Directives

Inline `# shellcheck disable=SCxxxx` suppressions are used for known-safe dynamic sources. Shellcheck runs in CI in advisory mode (`continue-on-error: true`) with `severity: warning`.

### Container Runtime Wrapping

**All** container invocations must go through `harness_docker` / `harness_docker_exec` from `scripts/lib/platform.sh` — never raw `docker`. This is enforced by `scripts/check_runtime_calls.sh`, which CI runs on every push.

```bash
# WRONG — breaks podman support
docker compose up -d

# CORRECT
harness_docker compose up -d
```

### Inline Comments

Section separators use `# ---`:

```bash
# --- section name ----------------------------------------------------------
```

Long inline comments explain non-obvious decisions inline above the code they describe. Comment blocks above functions explain purpose, arguments, and caveats — not a formal JSDoc style, but dense prose.

### Exit Codes

- `exit 0` — success
- `exit 1` — generic failure (also `return 1` for functions)
- Functions return failure, callers abort under `set -e`, or callers check `rc` explicitly

### Case Statements (dispatch)

Command dispatch uses `case` with aligned `;;` and aligned tab-indented labels. Subcommands have one-liner dispatch entries:

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

In-script heredocs use `<<'EOF'` (single-quoted, no variable expansion) for literal content such as test fixture files and help text. Variable-expanding content uses `<<EOF` (unquoted).

### Argument Parsing in Functions

Command functions parse their own flags via `while (( $# > 0 )); do case "$1" in ... esac; shift; done`. Boolean flags set local integer variables (`local check_only=0`).

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

Always quote variable expansions: `"$var"`, `"${var}"`, `"${array[@]}"`. Array expansions use `"${arr[@]}"` not `$arr`.

---

## Python Conventions (`proxy/proxy.py`, `proxy/test_proxy.py`)

### Type Annotations

All module-level constants and function signatures carry type hints:

```python
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
def _normalize_api_base(url: str) -> str:
def generate_ndjson(...) -> Iterable[str]:
```

`typing` imports: `Any`, `Dict`, `Iterable`, `List`, `Optional`, `Tuple`.

### Module-Level Constants

Project-managed constants that are NOT user knobs are prefixed with `_` and carry a block comment explaining why they exist and why they are not env vars:

```python
_CHANGE_SYSTEM_TO_USER: bool = True
_HYBRID_DETAIL_TOOLS: List[str] = ["task", "skill"]
_PROMPT_MODE: str = "hybrid"  # set in main() before serving
```

### Docstrings

Module-level docstring lists all env vars, their defaults, and which are REQUIRED. Function docstrings are single-line or short multi-line, describing behavior — not parameter/return annotations.

### Section Comments

Sections delimited with:

```python
# ---------------------------------------------------------------------------
# Section Name
# ---------------------------------------------------------------------------
```

### Error Handling

Flask route handlers catch `Exception` broadly, log the traceback, dump to the `99_Fatal_Error_*` file if `OUTPUT_DIR` is set, and return HTTP 502. HTTP error codes from upstream are forwarded verbatim with a `print()` to stderr.

### Imports

Standard library first, then third-party (`requests`, `flask`). `noqa: E402` suppressed on the `import proxy` line in `test_proxy.py` because env vars must be set before import.

---

## Doc-Update Discipline

Architecture docs under `architecture/` are updated **in the same commit** as code changes that alter covered behavior. The architecture router table in `CLAUDE.md` defines which doc covers which area. Docs are short and structural — they describe shape, contracts, and load-bearing invariants, not every function.

The inventory (`tests/INVENTORY.md`) and coverage map (`tests/COVERAGE.md`) must be updated when behaviors are added or removed. Stable IDs (`F###`, `P###`, `A###`, ...) are never reused or renumbered.

---

## Naming Patterns Summary

| Context | Pattern | Example |
|---------|---------|---------|
| Shell scripts | `<subject>_<verb>.sh` | `upgrade_actions.sh`, `net_helpers.sh` |
| Test scripts | `<name>_test.sh` | `harness_test.sh`, `upgrade_test.sh` |
| Unit tests | `unit_<subject>_test.sh` | `unit_workdir_test.sh` |
| Lib functions | `harness_<verb>` | `harness_docker`, `harness_realpath` |
| Netlib functions | `netlib_<verb>` | `netlib_validate_host` |
| CLI commands | `cmd_<noun>` | `cmd_start`, `cmd_upgrade` |
| Private helpers | `_<verb>` or `_<verb>_<noun>` | `_reap_jq_sidecar`, `_probe_upstream_auth` |
| Env overrides | `HARNESS_<UPPER>` | `HARNESS_INSTALL_ROOT` |
| Python constants (no-knob) | `_UPPER_SNAKE` | `_CHANGE_SYSTEM_TO_USER` |

---

*Convention analysis: 2026-06-02*
