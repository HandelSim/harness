# Coding Conventions

**Analysis Date:** 2026-06-08

Two languages dominate: Bash (the ~6500-line `harness` CLI plus `scripts/lib/*.sh`, `agents/*.sh`, `firewall/*.sh`, installer) and Python (`proxy/proxy.py`, the Flask translating proxy, plus `tests/mock_upstream.py`). Conventions below are split where the two diverge. All claims verified against current files unless marked *(inferred)*.

## Naming Patterns

**Bash files:**
- Top-level CLI: `harness` (no extension, shebang `#!/usr/bin/env bash`).
- Libraries: `scripts/lib/<topic>.sh` — `platform.sh`, `net_helpers.sh`, `upgrade_actions.sh`.
- Entrypoints: `agents/entrypoint.sh`, `firewall/init-firewall.sh`, `harness-install.sh`.

**Bash functions:**
- Public command dispatch: `cmd_<name>` (`cmd_host`, `cmd_test`, `cmd_benchmark`, `cmd_host_down`). Dispatched from the `main`-level `case` near `harness:6555` (`host) cmd_host "$@" ;;`).
- Subsystem-prefixed helpers group by area: `host_*` (host mode: `host_preflight`, `host_confirm_gate`, `host_proxy_ensure_venv`, `host_proxy_start`, `host_require_config` at `harness:2647-2860`), `netlib_*` (`net_helpers.sh`), `mcp_*` (`mcp_compose_files`, `mcp_services_of`, `mcp_runtime_status`), `harness_*` for cross-platform primitives in `platform.sh` (`harness_docker`, `harness_docker_exec`, `harness_realpath`, `harness_validate_mount`, `harness_container_runtime`).
- Internal/private helpers get a leading underscore: `_self_path`, `_probe_upstream_auth`, `_gate_on_upstream_auth`, `_update_check_and_banner`, `_ensure_jq_sidecar`, `_reap_jq_sidecar`, `_upgrade_confirm`, `_git_branches_diverged`, `_downgrade_target_tag`, `_parse_prompt_mode_flag`. Upgrade-action internals use a `_upg_` prefix (`_upg_atomic_mv`, `_upg_is_preserved`, `_upg_json_array`, `_upg_json_str`).
- Usage text is a function: `_harness_test_usage`, `_harness_benchmark_usage`.

**Bash variables:**
- Locals are `lower_snake_case`, always declared `local` inside functions (`local section="all"`, `local missing_dep=0`).
- Environment / config knobs are `UPPER_SNAKE_CASE` and namespaced `HARNESS_*` (`HARNESS_INSTALL_ROOT`, `HARNESS_ALLOWLIST_PATH`, `HARNESS_NET_OVERRIDES_PATH`, `HARNESS_REGISTRY_DIR`, `HARNESS_PROJECT_NAME`, `HARNESS_SOURCE_ONLY`, `HARNESS_YOLO`, `HARNESS_HOST_CONFIRM`, `HARNESS_RUN_SLOW`, `HARNESS_CONTAINER_RUNTIME`). Proxy/upstream knobs use `PROXY_*` and a few legacy names (`PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`, `MODEL_CONTEXT_LENGTH` with legacy alias `OLLAMA_CONTEXT_LENGTH`).

**Python (`proxy/proxy.py`):**
- Functions `lower_snake_case` (`extract_tool_calls_and_text`, `translate_history_and_apply_prompt`, `format_tools_to_text`, `make_chunk`, `_scan_balanced_json`, `_estimate_tokens`).
- Module-level config constants `UPPER_SNAKE_CASE` with leading underscore when internal/hardcoded: `_CHANGE_SYSTEM_TO_USER=True`, `_HYBRID_DETAIL_TOOLS=["task","skill"]`, `_HOST_OS`. These are deliberately *not* env knobs — see `proxy/test_proxy.py` `test_detail_tools_is_project_managed_constant`.
- Test classes `TestXxx` (unittest), test methods `test_<behavior>` (`proxy/test_proxy.py`).

## Code Style

**Bash:**
- `set -euo pipefail` at the top of every script (`harness:27`, `scripts/check_runtime_calls.sh:27`, all `tests/*_test.sh`). Code must be `pipefail`-safe: capture-then-check the return code rather than relying on `&&` chains where a non-zero is expected (`run_harness() { local rc=0; HARNESS_OUT=$(...) || rc=$?; return $rc; }`, `tests/unit_host_mcp_test.sh:46`).
- 4-space indent, no tabs.
- Self-location boilerplate is standardized: `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"` (every test + lib script). The CLI itself resolves `$0` through symlinks with a portable resolver (`harness_realpath`) rather than depending on `realpath` (so Git Bash / minimal images work — see `harness:1` header block and `_self_path`).
- Arrays for argv construction (`local -a candidates=()`, `COMPOSE=(docker compose ...)`); always expand quoted `"${arr[@]}"`.
- `mapfile -t` + `shopt -s nullglob` (in a subshell) for glob collection so a no-match yields an empty array, not a literal pattern (`cmd_test`, `harness:6212`).
- Quote all expansions (`"${!v:-}"`, `"$clone_dir/tests"`); use `${var:-default}` for optional env.
- Flag parsing is a hand-rolled `while (( $# > 0 )); do case "$1" in ... esac; shift; done` loop. Both `--flag value` and `--flag=value` spellings are supported (see `cmd_test` and `cmd_benchmark`, `harness:6142-6177`, `6372-6428`): the space form does `shift 2; continue`, the `=` form falls through to the trailing `shift`. Unknown flags print usage to stderr and `return 1`.

**Python:**
- Module docstring documents every env var consumed (`proxy/proxy.py:1-31`). Keep it current — it is the canonical env-var reference alongside `.env.example`.
- Standard library + Flask + `requests`; no heavyweight frameworks. Pure-function translation core (`extract_tool_calls_and_text`, `translate_history_and_apply_prompt`) is kept side-effect-free so `proxy/test_proxy.py` can unit-test it without a server.

## Import Organization

**Bash:**
- Libraries are sourced, not exec'd. `platform.sh` is the universal entry-point: tests source `tests/lib/test_helpers.sh`, which in turn sources `${REPO_ROOT}/scripts/lib/platform.sh` so individual tests get `harness_docker` etc. for free (`tests/lib/test_helpers.sh:24-26`).
- `upgrade_actions.sh` self-defines `harness_jq` if absent (`if ! declare -F harness_jq; then ... fi`) so it works sourced standalone (verified load-bearing by `tests/upgrade_test.sh` T9 / spot-check J2-2).
- Inline `# shellcheck disable=SCxxxx` directives sit immediately above the line they excuse (`SC1091` for sourced paths, `SC2206` for deliberate word-split globbing in `cmd_test`).

**Python:** stdlib first, then third-party (`flask`, `requests`). *(inferred — proxy is a single module.)*

## Error Handling

**Bash:**
- One shared `err()` helper (`harness:179`) writes to **stderr**; user-facing diagnostics never pollute stdout (important because many functions' stdout is captured — e.g. `harness_jq`, `agent_container_name`). Multi-line errors call `err` repeatedly with indented continuation lines and actionable fixes (`host_preflight`: `err "  install: apt-get install jq | brew install jq"`, `harness:2657`).
- Functions return status codes; the dispatch layer decides whether to `exit`. Pattern: predicates end with a bare boolean test as the last expression (`(( missing_dep == 0 ))` is the whole return value of `host_preflight`, `harness:2681`). Hard-fail config checks `exit 1` directly (`host_require_config`, `require_runtime_config`); gates `return 1` and let the caller `|| exit 1` (`cmd_host`: `host_preflight || exit 1; host_confirm_gate "$yolo" || exit 1`, `harness:3007-3013`).
- Confirmation gates read from `/dev/tty` (not stdin) so they survive piped invocation, and refuse to proceed non-interactively unless an explicit env bypass is set (`host_confirm_gate` reads `/dev/tty`, honors `HARNESS_HOST_CONFIRM=1`, `harness:2727-2745`; `_upgrade_confirm` similar with a `default` arg).
- Tri-state returns where a binary isn't enough: `_probe_upstream_auth` returns 0 (200), 1 (401/403), 2 (connection failure) — callers branch on all three (verified `tests/harness_test.sh` T0.4).
- Atomic writes for any user-config mutation: `mktemp` + `mv` (`netlib_add_host`, `net open`, `_upg_atomic_mv`) so a crash never leaves a half-written allowlist or overrides JSON.

**Python:**
- Upstream failures are translated, never leaked: 401/403 print the unlock URL and forward the status; connection failure and non-JSON surface as HTTP 502 to the agent (`proxy.py` P037-P042 behaviors). Fatal exceptions optionally dump to `OUTPUT_DIR/99_Fatal_Error_*` (P048).
- Secrets are redacted in logs (the startup banner prints `test...1234`, never the raw key — guarded by `tests/proxy_test.sh` Scenario F / P006).

## Logging

- **Bash:** plain `echo`/`printf` to **stderr** via `err()` and direct `>&2` redirects. No log framework. Banners (update-available, firewall-open warning, host-mode confirmation) are multi-line `echo ... >&2` blocks.
- **Python:** the proxy prints a structured startup banner (host:port, redacted key, prompt-mode, OUTPUT_DIR state) and per-request warnings; optional file dumps go to `OUTPUT_DIR` with numbered prefixes (`01_Inbound_Request_*` … `99_Fatal_Error_*`, monotonic counter + timestamp). Dumps are skipped silently when `OUTPUT_DIR` is unset.

## Comments

- Heavy **block comments** explain *why*, not *what* — especially for non-obvious platform/security decisions. The CLI opens with a full runtime-layout diagram (`harness:1-26`); `host_confirm_gate` carries a paragraph explaining why host mode's blast radius warrants a harder gate than `--net` (`harness:2705-2709`); `check_runtime_calls.sh:1-26` explains the entire rationale for the lint.
- Section banners delimit functional areas: `# --- self-locate -----...`, `# --- harness benchmark ----...`.
- Issue references are inline where a function encodes a bug fix: `(#76)`, `(#81)`, `(#94)`, `(#112)` appear next to the code and in test evidence.
- **Python:** module + function docstrings; the env-var contract lives in the module docstring (`proxy/proxy.py:1-31`).

## Function Design

- **Single responsibility, prefix-grouped.** Predicates return status (`_git_branches_diverged`, `harness_docker_running`); builders print to stdout (`agent_container_name`, `host_opencode_config` → `printf '%s/host/opencode.json' "$state_root"`, `harness:2622`); mutators perform atomic writes and `err` on failure.
- **Testability seams are deliberate.** `HARNESS_SOURCE_ONLY=1` lets tests `source` the wrapper and call individual functions without invoking `main` (F012). `ensure_services_up` / `harness_docker` are stubbed in non-docker tests. Pure-Python translation functions take plain dicts/lists so they unit-test without Flask.
- Parameters via positional `$1`/`$2` with `${1:-default}` defaults (`host_confirm_gate() { local yolo="${1:-0}"; ... }`).

## Module Design

- The CLI is one monolithic `harness` script with a single bottom `main` dispatch `case` (`harness:6555` region) — subcommands are `cmd_*` functions, not separate files. Cross-cutting primitives are factored into `scripts/lib/platform.sh`, `net_helpers.sh`, `upgrade_actions.sh`, each independently sourceable.
- No barrel files; bash "exports" are just defined functions in a sourced file.

## Mandatory Project Rules (enforced, not stylistic)

Checked in CI and/or repo policy — hard constraints when writing code:

- **No raw `docker`/`podman` calls.** Every runtime invocation goes through `harness_docker*` wrappers (`platform.sh`). `scripts/check_runtime_calls.sh` greps for a literal `docker ` token outside an allowlisted set (wrapper defs, pre-clone installer fallbacks, user-facing message strings, comments, `require_docker`'s defensive fallback) and fails CI on a violation. Tests are scanned too. Note: **host mode (`harness host`) is the deliberate exception** — it runs the proxy as a host process and opencode directly, with no container; it still routes nothing through docker.
- **Doc-update-in-same-commit.** When a change alters behavior covered by an `architecture/*.md` doc, the doc is updated in the same commit (CLAUDE.md "Doc-update rule"). The architecture router table maps subsystem → doc.
- **No hardcoded model/secret defaults.** `agent_model` reads `DEFAULT_MODEL_NAME` with no fallback literal (F145); empty when unset. Env vars drive all configurable values.
- **`bash -n` syntax check is blocking** in CI (lint job, on every shell script). **shellcheck is advisory** (`additional_files: harness harness-install.sh scripts/lib/platform.sh scripts/lib/net_helpers.sh scripts/lib/upgrade_actions.sh scripts/check_runtime_calls.sh`).
- The only project skill is `.claude/skills/first-principles/` — a decision-making aid, not a code-convention source.

---

*Convention analysis: 2026-06-08*
