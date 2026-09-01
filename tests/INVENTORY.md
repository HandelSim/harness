# Harness — Testable Behavior Inventory

This document enumerates every atomic, testable behavior of the harness project. Each row is one behavior with a stable ID. IDs are grouped by prefix:

- **F###** — CLI commands and flags (the `harness` wrapper)
- **P###** — Proxy translation behaviors (`proxy/proxy.py`)
- **A###** — Agent entrypoint (`agents/entrypoint.sh`)
- **M###** — MCP lifecycle (`harness mcp …`, compose, registry state)
- **N###** — Firewall guardrails (`firewall/init-firewall.sh`, network overrides)
- **U###** — Upgrade actions (`scripts/lib/upgrade_actions.sh`, manifest dispatch)
- **Pe###** — Persistence (where state lives, what survives, what is regenerated)
- **O###** — *(retired — the proxy now serves the OpenAI-compatible interface directly; the separate container is gone)*
- **I###** — Installer (`harness-install.sh`)
- **Ho###** — Host mode, containerless (`harness host`, the `host_*` helpers)
- **B###** — Bootstrap (`harness-bootstrap.sh`, the version-stable fetch/handoff entrypoint)
- **C###** — ChatGPT backend, CLI side (`harness chatgpt`, backend selection, per-backend config/fingerprint/dispatch helpers in the `harness` wrapper)

Rows are intended to be atomic: one behavior, one row. Compound behaviors are split.

---

## CLI commands and flags (F###)

| ID | Behavior |
|----|----------|
| F001 | `harness` with no args prints help and exits 0 |
| F002 | `harness help` prints help and exits 0 |
| F003 | `harness -h` prints help and exits 0 |
| F004 | `harness --help` prints help and exits 0 |
| F005 | Unknown top-level command prints `unknown command '<cmd>'` to stderr, prints help to stderr, exits 1 |
| F006 | `_self_path` resolves the wrapper's own path via `harness_realpath` to support symlink installs |
| F007 | `HARNESS_INSTALL_ROOT` env var overrides the default install-root inference |
| F008 | `HARNESS_ALLOWLIST_PATH` env var overrides the allowlist path passed into compose |
| F009 | `HARNESS_NET_OVERRIDES_PATH` env var overrides the net-overrides JSON path |
| F010 | `HARNESS_REGISTRY_DIR` env var overrides the MCP registry directory |
| F011 | `HARNESS_PROJECT_NAME` env var overrides the compose project name |
| F012 | `HARNESS_SOURCE_ONLY=1` allows tests to source the wrapper without invoking `main` |
| F013 | `require_runtime_config` fails fast when `.env` is missing under the install root |
| F014 | `require_runtime_config` fails fast when `.harness-allowlist` is missing under the install root |
| F015 | `harness_jq` uses the host `jq` binary when present |
| F016 | `harness_jq` falls back to a containerized `jq` when host `jq` is absent — via a per-invocation `docker exec` sidecar (`_ensure_jq_sidecar`), reaped on exit / before exec (`_reap_jq_sidecar`) and swept when stale (`_sweep_stale_jq_sidecars`) |
| F017 | `_update_check_and_banner` prints a banner when the local install is behind `origin/main` |
| F018 | `_update_check_and_banner` honors a short timeout on `git ls-remote` so it never blocks the CLI indefinitely |
| F019 | `_update_check_and_banner` falls back to a cached value when the network probe fails |
| F020 | `harness check-updates` prints "up to date" when local matches remote |
| F021 | `harness check-updates` prints an "update available" notice with remote SHA when behind |
| F022 | `harness check-updates` exits non-zero on network failure if no cached value exists |
| F023 | `harness start` launches the `proxy` (and `agent`) services via `docker compose up -d` |
| F024 | `harness start` aborts when `require_runtime_config` fails |
| F025 | `harness start` invokes `warn_if_firewall_open` and prints a banner when any host is in net-overrides |
| F026 | `harness start` writes a runtime override file (`state/.harness-runtime.yml`) before invoking compose |
| F027 | `harness start` gates on `_probe_upstream_auth` and prints unlock URL when upstream returns 401 |
| F028 | `harness down` runs `docker compose down` for the harness project |
| F029 | `harness down` does NOT remove user state under `state/agent/home` |
| F030 | `harness restart` runs `down` then `start` |
| F031 | `harness update` performs `git pull --ff-only` on the install root |
| F032 | `harness update` refuses to operate on a dirty working tree |
| F033 | `harness upgrade` (no flags) reads `scripts/upgrade-manifest.json` |
| F034 | `harness upgrade` invokes each manifest entry through `apply_upgrade_actions` |
| F035 | `harness upgrade --check` reports planned actions without writing anything |
| F036 | `harness upgrade --no-prompt` skips interactive confirmation |
| F037 | `harness upgrade --no-restart` skips automatic `restart` after applying actions |
| F038 | `harness upgrade --rebuild` forces `docker compose build --no-cache` before restart |
| F039 | `_upgrade_confirm` returns success when stdin says "y" |
| F040 | `_upgrade_confirm` returns failure when stdin says "n" or empty default |
| F140 | `_upgrade_confirm` empty answer (Enter) resolves to the optional `default` arg: "n" aborts, "y"/unset proceeds (back-compat for existing callers) |
| F141 | `_git_branches_diverged` returns success only when HEAD and `@{u}` have each diverged (ahead>0 AND behind>0); failure for up-to-date, behind-only, ahead-only, and no-upstream branches |
| F142 | `harness upgrade` / `harness update` offer a `git reset --hard @{u}` recovery on a diverged-history `--ff-only` failure (defaults to N); `--no-prompt`/CI never auto-resets, and non-divergence pull failures abort unchanged |
| F041 | `harness logs <service>` follows compose logs for the named service (e.g., `proxy`) |
| F042 | `harness logs` (no service) follows logs for all running services (primarily `proxy`) |
| F043 | Bare `harness` (no command), or `harness` with a leading agent flag, launches an opencode agent in the CWD (option C dispatch); an unknown bare word still errors |
| F044 | `harness opencode` launches the agent container with the `opencode` mode |
| F045 | `harness shell` launches the agent container with the `shell` mode and an interactive TTY |
| F046 | `harness --yolo` sets `HARNESS_YOLO=1` in the agent container env |
| F047 | `harness --net` enables the per-invocation net-override for this launch |
| F048 | `harness --mount <path>` adds a bind mount to the agent container |
| F049 | `harness --mount` rejects unsafe targets via `harness_validate_mount` |
| F050 | `--mount` is repeatable: multiple `--mount <path>` flags accumulate |
| F051 | `harness -p "<prompt>"` runs the agent in print mode |
| F052 | `harness --print "<prompt>"` is an alias for `-p` |
| F053 | `harness opencode -p "<prompt>"` runs opencode in print mode |
| F054 | `agent_container_name` produces a unique per-launch container name (`harness-<tool>-<rand>`), not derived from the directory |
| F055 | Two launches of the same tool from the same directory produce distinct container names (concurrent same-dir agents don't collide) |
| F143 | Interactive agent launch (`run_agent_interactive`, `cmd_shell`) runs docker as a child (not exec), prints the GitHub-issues footer to stderr after the session exits, and propagates the container's exit code |
| F144 | `-p`/print mode (`run_agent_print`) does NOT print the issues footer (kept clean for scripts/pipes) |
| F145 | The CLI's `agent_model` reads `DEFAULT_MODEL_NAME` (required, no hardcoded default): empty when the var is unset/blank, an explicit value passes through verbatim |
| F146 | `_downgrade_target_tag` from an untagged tip resolves to the latest reachable release tag |
| F147 | `_downgrade_target_tag` sitting exactly on a tag resolves to the prior tag in history (topological, two-number tags ok) |
| F148 | `_downgrade_target_tag` on the earliest tag returns non-zero with no output (no earlier tag to downgrade to) |
| F149 | `harness downgrade` confirm defaults to N — declining leaves HEAD unchanged and exits non-zero; accepting `git reset --hard`s the branch to the target tag |
| F056 | `harness list` lists currently running harness containers for this install root |
| F057 | `harness stop` stops the agent container without affecting `proxy` |
| F058 | `harness stop <name>` stops the named agent container |
| F059 | `pick_agent` prompts when multiple agents are running |
| F060 | `pick_agent` returns the single running agent without prompting |
| F061 | `pick_agent` errors when no agent is running |
| F063 | `harness net list` prints every host on the allowlist with `pull`/`push` direction |
| F064 | `harness net list` includes hosts annotated with `# git-push` as `push` direction |
| F065 | `harness net allow <host>` validates the host via `netlib_validate_host` |
| F066 | `harness net allow <host>` rejects uppercase letters |
| F067 | `harness net allow <host>` rejects leading dot |
| F068 | `harness net allow <host>` rejects trailing dot |
| F069 | `harness net allow <host>` rejects leading hyphen |
| F070 | `harness net allow <host>` rejects trailing hyphen |
| F071 | `harness net allow <host>` rejects consecutive dots |
| F072 | `harness net allow <host>` rejects characters outside `[a-z0-9.-]` |
| F073 | `harness net allow <host>` is idempotent (already-present pull entry: silent no-op) |
| F074 | `harness net allow --push <host>` adds a host with `# git-push` annotation |
| F075 | `harness net allow --push <host>` upgrades an existing pull entry to push |
| F076 | `harness net allow <host>` on an existing push entry does NOT downgrade (silent no-op) |
| F077 | `harness net deny <host>` removes the host line from the allowlist |
| F078 | `harness net deny <host>` is idempotent (not present: silent no-op) |
| F079 | `harness net edit` opens `$EDITOR` on the allowlist |
| F080 | `harness net status` shows allowlist size and open-override services |
| F081 | `harness net open <service>` requires typing the confirmation phrase "I understand the risks" |
| F082 | `harness net open <service>` rejects on phrase mismatch |
| F083 | `harness net open <service> --reason "<text>"` stores the reason in net-overrides JSON |
| F084 | `harness net open <service>` writes via atomic `mktemp + mv` to avoid partial writes |
| F085 | `harness net close <service>` removes the service from net-overrides JSON |
| F086 | `harness net close <service>` drops the JSON key entirely when no services remain |
| F087 | `harness net close <service>` is idempotent (not open: silent no-op) |
| F088 | `net_known_services` returns the list of services that have net-override support |
| F089 | `harness net open <unknown-svc>` errors with the known-services list |
| F151 | `harness net open <known-svc>` accepts the service: membership validation captures `net_known_services` once and matches with a here-string, so a slow producer (real `docker compose config`) does not SIGPIPE under `set -o pipefail` and falsely reject the first-listed service (e.g. `proxy`) |
| F090 | `harness unlock` prints the upstream unlock URL when probe returns 401 |
| F091 | `harness unlock` prints "already authorized" when probe returns 200 |
| F092 | `harness unlock` reports error when probe is unreachable |
| F093 | `_probe_upstream_auth` returns 0 on HTTP 200, 1 on 401/403, 2 on connection failure |
| F094 | `_gate_on_upstream_auth` runs the probe and aborts `start` on auth failure |
| F095 | `harness doctor` reports deps section: docker/podman + git + jq presence |
| F096 | `harness doctor` reports install section: install-root path, wrapper presence |
| F097 | `harness doctor` reports config section: `.env` and `.harness-allowlist` presence and parseability |
| F098 | `harness doctor` reports network section: PROXY_API_URL hostname allowlisted vs not |
| F099 | `harness doctor` reports storage section: state/output, state/agent/home writability |
| F100 | harness honors `HTTP_PROXY`/`HTTPS_PROXY` for host-side git calls; a non-empty value in `.env` wins, else the invoking shell's value (a blank `.env` value does not clobber the shell) |
| F101 | `harness_docker` / `harness_docker_exec` pass the host proxy (all four spellings) through to the container runtime so `compose build` / BuildKit routes image pulls and `RUN` steps through it; running containers never get it (compose declares no proxy vars) |
| F102 | `harness_normalize_proxy_env` mirrors the upper/lower-case proxy spellings without overwriting an explicit value |
| F100 | `harness doctor` reports runtime section: docker/podman daemon reachable |
| F101 | `harness doctor` reports images section: presence and age of `harness-proxy`, `harness-agents` |
| F102 | `harness doctor` reports mcp section: installed MCP services and enabled/disabled state |
| F103 | `harness doctor` reports agents section: any agent container currently running |
| F104 | `harness preflight` validates required commands exist (docker/podman, git) |
| F105 | `harness preflight` validates `.env` exists with non-empty `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME` |
| F106 | `harness preflight` validates the PROXY_API_URL hostname appears on the allowlist |
| F107 | `harness preflight` exits 0 on success |
| F108 | `harness preflight` exits non-zero with summary count on failure |
| F109 | `harness mcp` (no subcommand) prints MCP-specific help |
| F110 | `harness mcp list` lists registry entries with availability and installed state |
| F111 | `harness mcp install <name>` copies the registry source into `state/mcp/<name>` |
| F112 | `harness mcp install <name>` writes `state/mcp/<name>/harness-meta.json` with `enabled: true` |
| F113 | `harness mcp install <name>` errors on unknown registry entry |
| F114 | `harness mcp install <name>` errors on already-installed entry |
| F115 | `harness mcp uninstall <name>` removes `state/mcp/<name>/` entirely |
| F116 | `harness mcp uninstall <name>` errors on not-installed entry |
| F117 | `harness mcp enable <name>` flips `enabled: true` in `harness-meta.json` |
| F118 | `harness mcp enable <name>` errors on not-installed entry |
| F119 | `harness mcp disable <name>` flips `enabled: false` in `harness-meta.json` |
| F120 | `harness mcp disable <name>` errors on not-installed entry |
| F121 | `harness mcp up` starts every enabled MCP service via compose |
| F122 | `harness mcp up <name>` starts a single named MCP service |
| F123 | `harness mcp up <name>` errors when service is disabled |
| F124 | `harness mcp down` stops every running MCP service |
| F125 | `harness mcp down <name>` stops a single named MCP service |
| F126 | `harness mcp logs <name>` follows logs for the named service |
| F127 | `harness mcp status` prints runtime status for each installed MCP service |
| F128 | `mcp_compose_files` aggregates `compose.yml` paths from every enabled MCP service |
| F129 | `mcp_services_of` returns the compose service names declared by a given MCP entry |
| F130 | `mcp_runtime_status` returns running/stopped/unknown per service |
| F131 | `compose()` wrapper threads `HARNESS_PROJECT_NAME` through every compose invocation |
| F132 | `compose()` wrapper sets `MSYS_NO_PATHCONV=1` on Git Bash |
| F133 | `compose()` wrapper composes `-f docker-compose.yml` plus the runtime override file |
| F134 | `compose()` wrapper appends `-f` entries from `mcp_compose_files` when MCP services are enabled |
| F135 | `write_runtime_override` injects `HOST_UID`, `HOST_GID`, `HARNESS_HOST_CWD` into agent service env |
| F136 | `write_runtime_override` mounts the agent shared home from `state/agent/home` |
| F137 | `write_runtime_override` adds bind mounts requested via `--mount` |
| F138 | `warn_if_firewall_open` prints a yellow warning when any service is in net-overrides |
| F139 | `warn_if_firewall_open` is silent when net-overrides is empty or absent |
| F150 | `harness start/restart --prompt-mode <mode>` validates `hybrid`/`user_front`/`passthrough` (`_parse_prompt_mode_flag`) and injects `PROXY_PROMPT_MODE` onto the proxy via `write_runtime_override` (ephemeral, not persisted); folds into the proxy's firewall opt-out block when both apply |
| F152 | `seed_reminder_file` (called by `cmd_start` and `host_proxy_start`) copies the tracked `proxy/reminder.md` to the gitignored `<install_root>/reminder.md` — next to `.env` and `.harness-allowlist` — when it is missing, never overwriting an edited copy, and `rmdir`s an empty directory docker left at that path from a prior missing-mount-source `up`. No path variable is added for it: the copy keeps its tracked basename under `$INSTALL_ROOT`, which `compose()` already exports and `host_proxy_start` passes in the `nohup` env. The compose default stays `./proxy` so a bare `docker compose up` always has a real mount source |
| F153 | `_warn_unquoted_env_values` runs over `.env` BEFORE `source` and names every key whose value is unquoted and carries a space or one of `; & | ( ) < > \``. `source` ends the assignment there and runs the remainder, so a pasted cookie (`a=1; oai-did=2`) either kills every harness command with `oai-did=2: command not found` or is silently cut to the first pair and 401s at the upstream — while docker compose's dotenv parser reads the whole line, so the two consumers of `.env` disagree. Warns rather than aborts: `harness config set` is the repair and must stay reachable. Silent on comments, already-quoted values, `export KEY=`, inline `# comments`, empty values, and `$VAR` |
| F154 | `seed_tool_guidance_file` (called by `cmd_start` and `host_proxy_start`, a wrapper over the shared `seed_user_data_file` that also backs `seed_reminder_file`) copies the tracked `proxy/tool-guidance.json` to the gitignored `<install_root>/tool-guidance.json` when it is missing, never overwriting an edited copy, and `rmdir`s an empty directory docker left at that path. It rides on the same `INSTALL_ROOT` export as the reminder — no per-file path variable for either — and the compose default stays `./proxy` |
| F155 | `seed_user_data_file` migrates a copy left by either earlier layout: an existing `<install_root>/.harness-reminder.md` / `.harness-tool-guidance.json`, or one under the short-lived `<install_root>/.harness-data/`, is MOVED to `<install_root>/<tracked basename>` (user edits intact) instead of being shadowed by a freshly seeded default, and a legacy file is never allowed to overwrite the current copy. Runs on every `harness start`, so an install that never runs `harness upgrade` still migrates; an empty `.harness-data/` and an empty legacy directory left by an old bind-mount are `rmdir`d |

---

## Proxy behaviors (P###)

| ID | Behavior |
|----|----------|
| P001 | `GET /health` returns 200 with body `{"status":"ok"}` |
| P002 | Catch-all route accepts any path and forwards to upstream chat-completions |
| P003 | Proxy reads `PROXY_HOST` from env (default `0.0.0.0`) |
| P004 | Proxy reads `PROXY_PORT` from env (default `8000`) |
| P005 | Proxy reads `PROXY_API_URL` from env |
| P006 | Proxy reads `PROXY_API_KEY` from env |
| P007 | Proxy reads `DEFAULT_MODEL_NAME` from env (fallback model when a request omits one) |
| P008 | Proxy reads `PROXY_TIMEOUT` from env |
| P009 | Proxy reads `MODEL_CONTEXT_LENGTH` from env (legacy alias: `OLLAMA_CONTEXT_LENGTH`) |
| P010 | Proxy reads `PROXY_PROMPT_MODE` from the container env (default `hybrid`); not a `.env` knob — set only via `harness --prompt-mode` for benchmarking |
| P011 | The system→user conversion is governed by the hardcoded `_CHANGE_SYSTEM_TO_USER=True` constant (no longer read from an env var) |
| P012 | Proxy reads `OUTPUT_DIR` from env for debug dumps |
| P013 | `PROXY_PROMPT_MODE=user_front` injects the cooperative prompt before the latest user message |
| P017 | `PROXY_PROMPT_MODE=hybrid` (the default) puts full tool definitions at the stable prefix and a tool-name-list reminder on the latest user message |
| P018 | Unknown, absent, or removed `PROXY_PROMPT_MODE` (incl. legacy `user_front`/`user`/`system`/`user_bookend`) falls back to `hybrid` and warns |
| P019 | `extract_tool_calls_and_text` parses fenced ```json blocks from assistant text |
| P020 | `extract_tool_calls_and_text` extracts ALL `json` blocks, not just the first |
| P021 | `extract_tool_calls_and_text` preserves the textual order of tool_use blocks |
| P022 | `_scan_balanced_json` correctly handles strings containing escaped quotes |
| P023 | `_scan_balanced_json` correctly handles strings containing literal backticks |
| P024 | `_scan_balanced_json` returns failure on truncated JSON |
| P025 | Tool calls in assistant history are converted to fenced ```json markdown blocks for upstream |
| P026 | Tool results in user history are wrapped verbatim in `<<<BEGIN_TOOL_RESULT>>>` / `<<<END_TOOL_RESULT>>>` markers for upstream |
| P027 | `translate_history_and_apply_prompt` coalesces consecutive same-role messages |
| P028 | Multiple consecutive system messages are merged into one |
| P029 | Multiple consecutive user messages are merged into one |
| P030 | With `_CHANGE_SYSTEM_TO_USER` True (always, since it is a hardcoded constant), the system message is rewritten as a user message |
| P031 | When system→user rewrite happens, a stub assistant turn ("Understood…") is inserted so upstream sees user/assistant alternation |
| P032 | Streaming emits OpenAI SSE `chat.completion.chunk` objects (`data: {...}` lines) |
| P033 | SSE stream terminates with a literal `data: [DONE]` line; `finish_reason` (`stop`/`tool_calls`) replaces the old `done`/`done_reason` fields |
| P034 | A usage chunk (`usage` derived from upstream usage or fallback estimate) is emitted when `stream_options.include_usage` is set |
| P035 | Tool-call ids emitted by the proxy are prefixed `call_` and are unique per call |
| P036 | `_estimate_tokens` returns `len(text)//4` capped at `MODEL_CONTEXT_LENGTH` |
| P037 | Upstream 401 response prints the unlock URL and forwards 401 |
| P038 | Upstream 403 response prints the unlock URL and forwards 403 |
| P039 | Upstream 429 response prints a rate-limit warning and forwards 429 |
| P040 | Upstream 5xx response prints a warning and forwards the status |
| P041 | Upstream connection failure surfaces as HTTP 502 to the agent |
| P042 | Upstream non-JSON response surfaces as HTTP 502 to the agent |
| P043 | When `OUTPUT_DIR` is set, request dumps go to `01_Inbound_Request_*` files |
| P044 | When `OUTPUT_DIR` is set, upstream request dumps go to `02_API_Request_*` files |
| P045 | When `OUTPUT_DIR` is set, upstream OK response dumps go to `03_API_Response_*` files |
| P046 | When `OUTPUT_DIR` is set, upstream error dumps go to `03_API_Error_*` files |
| P047 | When `OUTPUT_DIR` is set, OpenAI output dumps go to `04_OpenAI_Response_*` (non-stream) / `04_OpenAI_SSE_Response_*` (stream) files |
| P048 | When `OUTPUT_DIR` is set, fatal exceptions dump to `99_Fatal_Error_*` files |
| P049 | Debug dump filenames embed a monotonic counter and timestamp |
| P050 | Debug dumps are skipped silently when `OUTPUT_DIR` is unset |
| P051 | Streaming requests yield OpenAI SSE (`data: {chat.completion.chunk}` lines) terminated by `data: [DONE]` |
| P052 | Non-streaming requests yield a single OpenAI `chat.completion` JSON object |
| P053 | Tool-result messages in the inbound payload are translated into user-role text, wrapped verbatim in `<<<BEGIN_TOOL_RESULT>>>` markers (content never parsed; agent-agnostic) |
| P054 | The cooperative prompt instructs the model to use ```json fenced blocks for tool calls |
| P055 | The cooperative prompt enumerates available tools by name and schema |
| P056 | Proxy forwards the inbound (requested) model to upstream VERBATIM (no tag stripping — `gpt-4:latest` goes out as `gpt-4:latest`); falls back to `DEFAULT_MODEL_NAME` only when the request omits a model |
| P057 | Tool-result name is resolved from metadata: explicit `tool_name`/`name` field, else `tool_call_id` correlated to the assistant `tool_calls`, else positional order, else `unknown_tool` |
| P058 | Hybrid mode echoes the full description of the tools named by `_HYBRID_DETAIL_TOOLS` (`tool-guidance.json`'s `detail_tools`, shipped default `["task","skill"]`, never an env var) into the recency reminder in `<<<BEGIN_TOOL_DETAIL>>>` blocks; only present, non-empty-description tools surface |
| P059 | The hybrid recency reminder advises the model to default to the listed tools over doing the work by hand, with concrete examples (`webfetch` vs curl/Python, `todowrite`/`todoread` vs a todo file) |
| P060 | Proxy `GET /v1/models` proxies the upstream models catalog: forwards `{base}/v1/models` with the bearer key and returns the upstream status/body verbatim (so a locked-key 401 + `unlock_url` passes through); the agent uses this route to enumerate available models |
| P061 | Proxy derives endpoints from `PROXY_API_URL` as a base — `{base}/v1/chat/completions` and `{base}/v1/models` — stripping a trailing `/v1/chat/completions`, `/chat/completions`, or `/v1` first |
| P062 | `_validate_config` refuses to start (exit 1) when `HARNESS_FORCE_LOOPBACK` is truthy (`1`/`true`/`yes`) but `PROXY_HOST` is not a loopback address (`127.0.0.1`/`::1`/`localhost`); container mode (var unset/falsey) may bind `0.0.0.0`. Belt-and-suspenders that host mode never exposes the keyed, firewall-less proxy off-box |
| P063 | `_force_utf8_stdio` (called first in `main()`) reconfigures stdout/stderr to `encoding="utf-8", errors="backslashreplace"` so a non-ASCII print can never crash the proxy. Regression for the Windows host-mode crash: redirected stdout defaults to cp1252, which cannot encode the `→` (U+2192) in the `sys→user:` startup banner, so `print` raised `UnicodeEncodeError` and killed the proxy at startup. No-op on streams already UTF-8 or lacking `reconfigure` (Python < 3.7) |
| P064 | The hybrid recency reminder's prose is loaded from a FILE, not a literal: `_setup_reminder_template` reads `_reminder_template_path()` (`$INSTALL_ROOT/reminder.md`, else `reminder.md` beside `proxy.py`), strips a leading `<!-- -->` header block and trailing newlines, and `build_cooperative_prompt_hybrid_reminder` substitutes `{{HOST_OS}}` / `{{CWD}}` / `{{TOOL_ENTRIES}}` by `str.replace` (never `format`, so a user edit cannot raise; unknown tokens stay literal). A missing, directory-shadowed, or empty file logs `[!]` and falls back to `_REMINDER_FALLBACK`, which keeps the tool-call envelope so tool calling still works |
| P065 | `_chatgpt_flatten_messages` renders the OpenAI-shaped history into the backend-api's single-message dialect: a lone user turn passes through verbatim, multi-turn history is labeled `User:`/`Assistant:` (not dropped) and order-preserved, blank messages are dropped, an empty history yields `""`, and list-shaped `content` blocks are flattened rather than crashing |
| P066 | `_chatgpt_collect_stream` accumulates `message_delta.delta` chunks across the SSE stream into the final answer text |
| P067 | `_chatgpt_collect_stream` treats `message.content.parts` as cumulative (each event repeats everything so far) and takes only the new suffix, rather than duplicating it |
| P068 | `_chatgpt_collect_stream` stops at a literal `data: [DONE]` line, discarding anything sent after it |
| P069 | `_chatgpt_collect_stream` skips blank lines, non-`data:` lines (e.g. `event: ping`), and malformed JSON payloads without raising |
| P070 | `_chatgpt_collect_stream` decodes `bytes` SSE lines the same as `str` lines |
| P071 | `_chatgpt_post` POSTs to the hardcoded backend-api conversation-stream endpoint (`CHATGPT_STREAM_URL`) with `stream=True` |
| P072 | `_chatgpt_post`'s request body matches the backend-api dialect: `action: "next"`, `model` from `CHATGPT_MODEL_NAME`, a hardcoded `timezone`/`timezone_offset_min`, and the flattened history as a single `author: {"role": "user"}` message with `content.content_type: "text"` |
| P073 | `_chatgpt_post` never sends `conversation_id`/`parent_message_id` — the proxy is stateless and resends the full history every turn, so no server-side conversation state is carried forward |
| P074 | `_chatgpt_post` sends the cookie, `Origin`, `Referer`, and a browser `User-Agent` header (impersonating the web client) alongside `Accept: text/event-stream` |
| P075 | `_chatgpt_post` returns an OpenAI-shaped `chat.completion` response object (real JSON in both `.json()` and `.text`) so downstream extraction (`extract_assistant_content`, `_extract_finish_reason`) works unchanged |
| P076 | `_chatgpt_post` surfaces a non-200 upstream status with the upstream response body folded into `error.message` |
| P077 | `_upstream_post` routes to the OpenAI `CHAT_URL` when `PROXY_BACKEND=openai` (unchanged default path) |
| P078 | `_upstream_post` routes to `_chatgpt_post` when `PROXY_BACKEND=chatgpt` |
| P079 | The chatgpt backend only replaces the outbound call: end-to-end through `/v1/chat/completions`, a plain chatgpt-backend answer reaches the client, cooperative fenced tool calls are still extracted, and an upstream error becomes a 502 |
| P080 | `GET /v1/models` on the chatgpt backend synthesizes a one-model catalog from `CHATGPT_MODEL_NAME` without calling upstream; the openai backend still proxies the real upstream catalog |
| P081 | `_validate_config` for `PROXY_BACKEND=chatgpt` requires only `CHATGPT_BASE_URL`/`CHATGPT_MODEL_NAME`/`CHATGPT_COOKIE_STRING` (not the openai `PROXY_API_*`/`DEFAULT_MODEL_NAME` trio); a missing cookie is fatal, an unknown `PROXY_BACKEND` value is fatal, and the openai backend still requires its own trio |
| P082 | `_chatgpt_collect_stream` accepts `data:` with no following space as a valid SSE event (`data:{...}` is legal SSE; a `startswith("data: ")` test dropped it silently) |
| P083 | `_chatgpt_collect_stream` returns `(text, events, prefix)` — the event COUNT distinguishes "this 200 was not an event stream at all" (zero events) from "the model genuinely said nothing" (events seen, empty text) |
| P084 | A snapshot event whose `message.author.role` is not `assistant` cannot clobber the accumulated answer (blind "longest wins" let a long tool/reasoning message replace the real reply) |
| P085 | A snapshot event whose `message.content.content_type` is not `text` (e.g. `thoughts`) is skipped |
| P086 | An unlabelled snapshot — no `author.role` and no `content_type` — is still accepted (the reference client filtered on neither, so dropping these would break the shape it verified) |
| P087 | `_chatgpt_post` passes `allow_redirects=False` and surfaces a 3xx as a 502 naming the `Location`, rather than following it: `requests` strips the `Cookie` header across a redirect and turns the POST into a GET, so a followed redirect arrives unauthenticated |
| P088 | A 200 that is not an event stream (a login/interstitial page) is a 502 naming the response prefix, not an empty completion — an empty completion drove the agent into its empty-response rescue loop |
| P089 | A stream that genuinely produced events but no text is still a 200 with an empty assistant message (not conflated with P088's zero-event case) |
| P090 | `_chatgpt_origin()` is computed per call from `CHATGPT_BASE_URL` (so it tracks a changed base URL) and strips any path prefix: `https://chat.example.com/api` yields `Origin: https://chat.example.com` and `Referer: https://chat.example.com/` |
| P091 | `PROXY_PROMPT_MODE=passthrough` is fatal on the chatgpt backend (`_validate_config` exits) because the backend-api dialect has no tools field — passthrough would silently drop every tool schema and tool result; the openai backend still accepts it |
| P092 | The hybrid recency reminder's per-tool entries are loaded from a FILE, not literals: `_tool_guidance_path()` resolves `$INSTALL_ROOT/tool-guidance.json` (via the shared `_user_data_path`, which also resolves the reminder — the already-exported `INSTALL_ROOT` names the directory for both, and wins outright when set rather than falling through on a missing file) and falls back to `tool-guidance.json` beside `proxy.py`; `_load_tool_guidance` parses the JSON and returns `(values, warnings)` with PER-SECTION fallback, so a wrong type, an empty legend, or a non-string description drops only that piece (each named in a `[!]` line, with line/column for a JSON syntax error) while the rest of the file still loads; a missing/unreadable/non-object file falls back to a built-in legend with NO guidance map so every tool renders as a bare signature; `_README` and any other unnamed key is ignored |

---

## Agent entrypoint (A###)

| ID | Behavior |
|----|----------|
| A001 | Root-side init runs `/usr/local/bin/init-firewall.sh` unless `HARNESS_FIREWALL_DISABLED=1` |
| A002 | When `HARNESS_FIREWALL_DISABLED=1`, firewall init is skipped and a warning is printed |
| A003 | Root-side init remaps the `harness` user UID to `HOST_UID` env value |
| A004 | Root-side init remaps the `harness` group GID to `HOST_GID` env value |
| A005 | UID/GID remap is skipped if the user already has the target uid/gid |
| A006 | `chown` of `/home/harness` is skipped when ownership already matches the target |
| A007 | Root-side init drops privileges to `harness` via `gosu` before exec'ing the agent |
| A008 | User-side init calls `configure-git-credentials.sh` before the agent runs |
| A009 | User-side init seeds `/etc/skel/harness/.` into `$HOME` once (`cp -an`) so it never overwrites user edits |
| A010 | User-side init `cd`s into `HARNESS_HOST_CWD` if set |
| A018 | `ensure_opencode_config` writes `~/.config/opencode/opencode.json` on every launch |
| A019 | `ensure_opencode_config` configures a `harness` provider pointing at the proxy's OpenAI-compatible endpoint |
| A020 | `ensure_opencode_config` defines a `yolo` agent profile |
| A035 | `ensure_opencode_config` sets the opencode provider display name to the fixed string `GenAI Harness` |
| A036 | `ensure_opencode_config` builds the opencode model list from the proxy `GET /v1/models` response, always includes `DEFAULT_MODEL_NAME`, and selects `harness/${DEFAULT_MODEL_NAME}` |
| A021 | `merge_opencode_mcp_servers` translates the canonical `mcpServers` JSON into opencode's mcp shape |
| A022 | `merge_opencode_mcp_servers` distinguishes `local` (stdio) from `remote` (SSE/HTTP) entries |
| A028 | `run_opencode` sets `OPENCODE_DISABLE_AUTOUPDATE=1` |
| A029 | `run_opencode` passes `--agent yolo` when `HARNESS_YOLO=1` |
| A030 | `run_opencode` invokes `opencode run "<prompt>"` when `-p` is passed |
| A031 | `run_opencode` strips the `-p`/`--print` flag before forwarding remaining args |
| A032 | `run_shell` execs `bash -l` so login dotfiles run |
| A033 | Entry dispatch chooses opencode/shell based on the first positional arg (default opencode) |
| A034 | Unknown mode in dispatch errors with usage |

---

## MCP lifecycle (M###)

| ID | Behavior |
|----|----------|
| M001 | An MCP entry under `mcp-registry/<name>/` is "available" until installed |
| M002 | `harness mcp install <name>` copies `mcp-registry/<name>/` to `state/mcp/<name>/` |
| M003 | After install, `state/mcp/<name>/harness-meta.json` exists with `{"enabled": true}` |
| M004 | `harness mcp install` is rejected if `state/mcp/<name>/` already exists |
| M005 | `harness mcp uninstall` removes `state/mcp/<name>/` recursively |
| M006 | `harness mcp uninstall` is rejected if `state/mcp/<name>/` does not exist |
| M007 | `harness mcp enable` only flips meta; it does NOT start the service |
| M008 | `harness mcp disable` only flips meta; it does NOT stop the service |
| M009 | `harness mcp up` only starts services with `enabled: true` |
| M010 | `harness mcp up` skips services without a `compose.yml` |
| M011 | `harness mcp down` stops services regardless of enabled state |
| M012 | The `mcp` compose profile gates MCP services from normal `harness start` |
| M013 | `mcp_compose_files` returns files only for enabled installed services |
| M014 | `mcp_services_of <name>` parses service names from the entry's `compose.yml` |
| M015 | `mcp_runtime_status` returns `running` when the container is up |
| M016 | `mcp_runtime_status` returns `stopped` when the container exists but is down |
| M017 | `mcp_runtime_status` returns `not_created` when the container does not exist |
| M018 | Re-installing a previously-uninstalled MCP entry starts with `enabled: true` again |
| M019 | Serena registry compose mounts `HARNESS_PROJECTS_ROOT` as `/workspaces/projects:ro` |
| M020 | Serena registry compose mounts `state/mcp/serena/data` as `/root/.serena` for index persistence |
| M021 | Serena registry compose exposes port 9121 SSE |
| M022 | Serena registry compose mounts the allowlist file |
| M023 | `state/agent/home/.harness-mcp-servers.json` is regenerated each agent launch (not user-managed) |
| M024 | `harness mcp register <name> --from <dir>` materializes `state/mcp/<name>/` (compose.yml + client-config.json + `harness-meta.json` with `{"enabled": true}`) and creates `data/` |
| M025 | `harness mcp register` discards the hidden `.staging-<name>/` and changes nothing in `state/mcp/` when the staged compose fails to merge into the harness graph (malformed/invalid snippet → non-zero exit) |
| M026 | `harness mcp register` rejects a staged snippet whose service name already exists in the merged compose graph (compose would silently override it) |
| M027 | `harness mcp register --no-enable` lands `{"enabled": false}`; the entry is excluded from `mcp_compose_files` until `harness mcp enable` flips it |
| M028 | An enabled registered MCP appears in `mcp_compose_files` output and its `client-config.json` is merged into `.harness-mcp-servers.json` |
| M029 | `harness mcp register` refuses an already-installed name without `--force`; `--force` re-materializes while preserving `data/` |
| M030 | `harness mcp register` prints `allowed_domains` → `harness net allow` recommendations and does NOT modify the allowlist (shared `mcp_print_firewall_recs`) |
| M031 | `harness mcp uninstall <registered-name> --force` removes config but preserves `data/` (uninstall is the inverse of register) |
| M032 | `harness mcp register <name>` prints a shadow warning when `<name>` also names a `mcp-registry/` entry |
| M033 | `harness mcp register --from <git-url> --ref <ref>` clones the source into `state/mcp/<name>/repo/` at the pinned ref and records `repo_clone_url`/`repo_clone_ref` provenance into `harness-meta.json` |

---

## Firewall guardrails (N###)

| ID | Behavior |
|----|----------|
| N001 | `init-firewall.sh` is skipped when `HARNESS_FIREWALL_DISABLED=1` |
| N002 | `init-firewall.sh` aborts with a clear error when required tools are missing (iptables, ipset, dig, curl, jq, awk, ip) |
| N003 | `init-firewall.sh` preserves existing Docker DNS chain rules across flush |
| N004 | `init-firewall.sh` always allows the loopback interface (lo) |
| N005 | `init-firewall.sh` always allows DNS over UDP port 53 |
| N006 | `init-firewall.sh` always allows DNS over TCP port 53 |
| N007 | `init-firewall.sh` always allows outbound SSH (port 22) |
| N008 | `init-firewall.sh` allows the host's /24 network |
| N009 | `init-firewall.sh` fetches GitHub IP ranges from `api.github.com/meta` (best-effort) |
| N010 | `init-firewall.sh` resolves each allowlist hostname via `dig` |
| N011 | `init-firewall.sh` adds resolved IPs to the `allowed-domains` ipset |
| N012 | Allowlist parsing strips inline `#` comments (e.g., `host # git-push` → `host`) |
| N013 | Allowlist parsing skips blank lines |
| N014 | Allowlist parsing skips full-line comments |
| N015 | `init-firewall.sh` accepts ESTABLISHED,RELATED connections |
| N016 | `init-firewall.sh` REJECTs with `icmp-admin-prohibited` so callers get fast failure |
| N017 | Default OUTPUT policy is DROP |
| N018 | `init-firewall.sh` fatally errors when `PROXY_API_URL` hostname is not on the allowlist (only for hosts with dots and not intra-cluster names) |
| N019 | `init-firewall.sh` verifies `example.com` is blocked after setup |
| N020 | `init-firewall.sh` verifies at least one of api.github.com / pypi.org / registry.npmjs.org is reachable after setup |
| N021 | `init-firewall.sh` errors if both verification probes fail |
| N022 | `configure-git-credentials.sh` sets global `credential.helper /bin/false` (block by default) |
| N023 | `configure-git-credentials.sh` parses `# git-push` annotations from the allowlist |
| N024 | `configure-git-credentials.sh` sets `credential.https://<host>.helper store` for each push-allowed host |
| N025 | `harness net open <service>` writes `.harness-net-overrides.json` so the next `start` mounts the service network |
| N026 | Net overrides JSON survives `harness down` (lives under install root, not `state/`) |
| N027 | `harness net status` lists each open override and its `reason` field |
| N028 | `netlib_add_host` writes via atomic `mktemp + mv` to prevent partial allowlist files |
| N029 | `netlib_add_host` upgrading from pull to push rewrites the line with `# git-push` annotation |
| N030 | `netlib_remove_host` removes the entire line including any inline annotation |
| N031 | `init-firewall.sh` is FATAL when the active upstream URL var is set but no hostname parses out of it (a scheme-less value such as `api.example.com/v1`): it names the variable and the `harness config set <var> <url>` fix and exits 1, instead of silently skipping the allowlist guard and failing later with an opaque connect error |

---

## Upgrade actions (U###)

| ID | Behavior |
|----|----------|
| U001 | `apply_upgrade_actions` dispatches each manifest action to the matching `upgrade_<type>` function |
| U002 | Unknown action `type` is reported as an error and counted in the summary |
| U003 | `upgrade_envfile_merge` appends new keys from source `.env.example` into the target `.env` |
| U004 | `upgrade_envfile_merge` preserves existing values for keys already present in the target |
| U005 | `upgrade_envfile_merge` prepends an "# Added by harness upgrade on <date>" comment marker |
| U006 | `upgrade_envfile_merge` carries forward preceding comment context from the source file |
| U007 | `upgrade_envfile_merge` honors `HARNESS_UPGRADE_DATE` env to make the marker deterministic in tests |
| U008 | `upgrade_envfile_merge` rejects lines with backslash continuation (refuses to inherit a multi-line value) |
| U009 | `upgrade_envfile_merge` emits a JSON result blob with added/skipped key counts |
| U010 | `upgrade_envfile_merge` strips CR characters from CRLF source files |
| U011 | `upgrade_linefile_merge` appends new lines that are not already present in the target |
| U012 | `upgrade_linefile_merge` ensures the target ends with a newline before appending |
| U013 | `upgrade_linefile_merge` skips lines that are already present (case-sensitive match) |
| U014 | `upgrade_linefile_merge` warns when an inline annotation in the source differs from the existing target entry |
| U015 | `upgrade_linefile_merge` preserves existing inline annotations (e.g., `# git-push`) on entries already in the target |
| U016 | `upgrade_directory_overwrite` uses `rsync -a -I` when rsync is available |
| U017 | `upgrade_directory_overwrite` falls back to pure-shell copy when rsync is absent |
| U018 | `upgrade_directory_overwrite` preserves paths listed in `preserve` |
| U019 | `upgrade_directory_overwrite` `_upg_is_preserved` matches an exact path |
| U020 | `upgrade_directory_overwrite` `_upg_is_preserved` matches a parent directory |
| U021 | `upgrade_directory_overwrite` `_upg_is_preserved` matches a bare directory name relative to root |
| U022 | `upgrade_directory_overwrite` reports `target_missing` when target dir doesn't exist and `condition: installed` |
| U023 | `upgrade_directory_overwrite` reports `source_missing` when source dir doesn't exist in the new tree |
| U024 | `_upg_atomic_mv` performs an atomic rename within the same filesystem |
| U025 | `harness_jq` fallback path also works inside `upgrade_actions.sh` (it is reused) |
| U026 | Manifest `version: 1` is required; future versions are rejected |
| U027 | Manifest `actions[]` runs before `registry_actions[]` |
| U028 | `registry_actions` with `condition: installed` skip when the MCP entry is not installed |
| U029 | `apply_upgrade_actions` returns non-zero when any action returns non-zero |
| U030 | `upgrade_userfile_sync` asks nothing and reports `skipped`/`identical` when the shipped source and the user's target are byte-identical, so a user who never edited the file sees no new upgrade question |
| U031 | `upgrade_userfile_sync` asks once when the bytes differ (printing both paths and the line delta), defaults to NO (bare Enter keeps the user's copy), and leaves the target byte-identical on a decline (`skipped`/`declined`, no `.bak` written) |
| U032 | `upgrade_userfile_sync` on an explicit yes copies the target to `<target>.bak` first, then installs the source atomically (`.tmp` + rename), and reports the basename in `files_updated`; a failed backup aborts the replacement (`backup_failed`, rc 1) |
| U033 | `upgrade_userfile_sync` never writes without an interactive yes: `dry_run` (`--check`), `allow_prompt=0` (`--no-prompt`) or no tty, `target_missing` (seeding's job), and `source_missing` each skip with that reason. `upgrade_userfile_needs_sync` mirrors the same both-exist-and-differ rule so the aggregate confirm lists the file only when a question is coming |

---

## Persistence (Pe###)

| ID | Behavior |
|----|----------|
| Pe001 | `.env` (user config) lives at install-root and survives `down`/`restart`/`upgrade` |
| Pe002 | `.harness-allowlist` (user config) lives at install-root and survives `down`/`restart`/`upgrade` |
| Pe003 | `.harness-net-overrides.json` lives at install-root and survives `down` |
| Pe004 | `state/agent/home/` is a per-install shared agent home, persisted across agent runs |
| Pe006 | `state/output/` is the proxy debug-dump directory; never managed by upgrades |
| Pe007 | `state/mcp/<name>/` holds installed MCP service state |
| Pe008 | `state/mcp/<name>/harness-meta.json` holds enabled/disabled state and is preserved by `directory_overwrite` upgrades |
| Pe009 | `state/mcp/serena/data/` (Serena index) is preserved by upgrades |
| Pe010 | `state/.harness-runtime.yml` is regenerated on every compose invocation |
| Pe011 | `state/agent/home/.harness-mcp-servers.json` is regenerated on every agent launch |
| Pe014 | `state/agent/home/.bashrc`, `.gitconfig` etc are seeded once from `/etc/skel/harness/.` then user-managed |
| Pe015 | `harness down` does not delete any `state/` content |
| Pe016 | `harness upgrade` does not delete any `state/` content |
| Pe017 | `harness mcp uninstall <name>` deletes `state/mcp/<name>/` (the only path that removes state) |
| Pe018 | `harness-meta.json` survives `directory_overwrite` even when the registry source no longer ships one |
| Pe019 | `data/` subdirs of MCP installs survive `directory_overwrite` via the manifest `preserve` list |

---

## Installer (I###)

| ID | Behavior |
|----|----------|
| I001 | `harness-install.sh` detects whether it was sourced or executed |
| I002 | Sourced invocation does not call `exit` (sets a flag and returns) |
| I003 | Preflight verifies `git` is on PATH |
| I004 | Preflight verifies `docker` or `podman` is on PATH |
| I005 | Preflight verifies `docker compose` or `podman compose` works |
| I006 | Preflight verifies at least 5 GB of free disk on the install destination |
| I007 | Preflight verifies the current working directory is writable |
| I008 | Preflight refuses to overwrite an existing `./harness` directory |
| I009 | `REPO_URL` defaults to `https://github.com/HandelSim/harness` |
| I010 | `HARNESS_REPO_URL` env var overrides the default repo URL |
| I011 | Inline platform fallbacks exist for `harness_realpath` and `harness_detect_os` before `platform.sh` is available |
| I012 | After clone, the installer sources the real `scripts/lib/platform.sh` |
| I013 | On Windows the installer runs a `dos2unix` pass over the cloned files |
| I014 | The installer creates `state/output/` |
| I015 | The installer creates `state/agent/home/` |
| I017 | The installer creates `state/mcp/` |
| I018 | If `.env` already exists in the install root, it is left untouched |
| I019 | If `$cwd/.env` exists before install, it is moved into the install root |
| I020 | Otherwise `.env` is seeded from `.env.example` |
| I021 | `.harness-allowlist` is seeded from `.harness-allowlist.example` when missing |
| I022 | The installer writes a `harness` wrapper at `$HOME/.local/bin/harness` |
| I023 | The wrapper is a real file, not a symlink |
| I024 | The wrapper hard-codes the install-root path |
| I025 | The installer updates PATH in `~/.bashrc` when `$SHELL` is bash |
| I026 | The installer updates PATH in `~/.zshrc` when `$SHELL` is zsh |
| I027 | The installer updates PATH in `~/.config/fish/config.fish` when `$SHELL` is fish |
| I028 | For other shells the installer prints a manual PATH instruction |
| I029 | The installer is idempotent on PATH: re-running does not duplicate the export line |
| I030 | After install, `harness preflight` is expected to succeed (smoke step in installer flow) |
| I031 | `harness_validate_mount` from `platform.sh` is available after install |
| I032 | `harness_container_runtime` resolves to docker or podman per `HARNESS_CONTAINER_RUNTIME` env |
| I033 | `harness_container_runtime` caches its resolved value within a single invocation |
| I034 | `harness_docker_path` translates POSIX paths to Windows form via `cygpath -m` on Git Bash |
| I035 | `harness_abs_path` translates `/c/Users/...` form back to POSIX on Git Bash |
| I036 | `harness_validate_mount` rejects `/`, `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/var`, `/dev`, `/proc`, `/sys`, `/root`, `/home/harness`, `/etc/harness`, `/workspace` |
| I037 | `harness_docker_running` returns success when the daemon responds to `docker info` |
| I038 | `harness_start_docker_desktop` polls up to 90 s for the daemon when running on macOS/Windows |
| I039 | `harness_require_docker` fails fast on Linux when the daemon is down (no auto-start attempt) |
| I040 | Preflight primitive `harness_check_command` exits non-zero with a clear message when a command is missing |
| I041 | Preflight primitive `_disk_space` returns failure when free space is below the threshold |
| I042 | Preflight primitive `_dir_writable` returns failure when the directory cannot accept a tempfile |
| I043 | When `HTTP_PROXY`/`HTTPS_PROXY` are exported in the installing shell, the installer persists them into the seeded `.env`, filling only blank lines (a pre-placed value wins) |
| I044 | On Windows Git Bash the installer bridges `~/.bash_profile` -> `~/.bashrc` so login shells pick up the PATH export |
| I045 | The `.bash_profile` bridge preserves a pre-existing `~/.profile` when it has to create `~/.bash_profile`, and is idempotent |
| I046 | A failed `git clone` is detected via its exit code and aborts the install with an actionable message; the installer never prints `✓ cloned` or `install complete` past a failed clone |
| I047 | Fatal errors (preflight failure, pre-existing install root, failed clone, missing `platform.sh`) abort even when the installer is `source`d — the terminating `return`/`exit` runs at the script's top level, so a sourced run does not continue past a fatal error |
| I048 | The initial clone takes its proxy from `HTTP_PROXY`/`HTTPS_PROXY` in a `.env` placed beside the installer when set (exported in both upper- and lower-case for git's libcurl), falling back to the host shell's exported proxy when the `.env` value is blank/absent |
| I049 | `harness_container_workdir` is a pass-through on Linux/macOS and prefixes with `//` on Windows Git Bash so MSYS does not rewrite the docker `-w` arg (issue #112) |
| I050 | The docker wrappers (`harness_docker`, `harness_docker_winpty`, `harness_docker_exec`, `harness_runtime_tty_ok`) export MSYS_NO_PATHCONV=1 AND MSYS2_ARG_CONV_EXCL='*' into bash's own env (via `local -x` / `export`) on Windows so MSYS argv conversion does not rewrite container-internal paths or path-list-convert `-v src:tgt` composites; pass-through on Linux/macOS (issue #112 root cause) |
| I051 | `harness_add_bind_mount` appends a single `--mount=type=bind,source=,target=[,readonly]` token on Windows (no bare `:` composite for MSYS/winpty path-list conversion to mangle into `invalid mode: …`) and the classic `-v src:tgt[:ro]` two tokens on Linux/macOS; used for all agent/shell bind mounts at the three launch sites (issue #112 winpty path) |

## Bootstrap (B###)

Behaviors of `harness-bootstrap.sh`, the thin version-stable entrypoint that
fetches the current `harness-install.sh` and hands off to it. Driven
docker-free and network-free by pointing `HARNESS_REPO_URL` at a local stub
"repo".

| ID | Behavior |
|----|----------|
| B001 | Resolves its own directory (the bundle dir holding `.env`/`.harness-allowlist`) from `BASH_SOURCE`, falling back to `$PWD` |
| B002 | Reads `HTTP_PROXY`/`HTTPS_PROXY` from the bundled `.env` and exports them (upper- and lower-case) for the fetch; a blank/absent value leaves the host's exported proxy untouched |
| B003 | Fetches the current `harness-install.sh`: copies straight out of the tree when `HARNESS_REPO_URL` is a local path, else fetches the raw script from `raw.githubusercontent.com/<slug>/<ref>/` via curl or wget |
| B004 | `HARNESS_INSTALL_REF` pins the fetched ref (default `main`); `HARNESS_REPO_URL` overrides the repo/fork |
| B005 | Shebang sanity check: a fetched file whose first line is not `#!` (e.g. a captive-portal HTML 200) is rejected |
| B006 | On fetch/validation failure, falls back to a bundled `harness-install.sh` if present (printing a notice), else aborts; the abort terminates a sourced run too (`return`/`exit` at top level) |
| B007 | The fetched installer lands in the bundle dir (as `.harness-install.fetched.sh`) so its `$script_dir` resolves to the bundle dir and it finds `.env`/`.harness-allowlist` beside it |
| B008 | Hands off by `source`ing the installer when itself sourced (PATH export reaches the user's shell) and returns its rc; executes it as a child and exits its rc otherwise |
| B009 | Only enables `set -euo pipefail` when executed; a sourced run does not leak strict mode into the user's interactive shell |
| B010 | Removes the fetched temp after handoff; never removes a bundled `harness-install.sh` (name does not match the temp) |

## Host mode — containerless (Ho###)

Behaviors of the `harness host` path (host_* helpers in `harness`). These run
without docker, network, or a real proxy/opencode spawn.

| ID | Behavior |
|----|----------|
| Ho001 | `host_require_config` fails (exit 1) when a required var (`PROXY_API_URL`/`PROXY_API_KEY`/`DEFAULT_MODEL_NAME`) is empty, naming the missing var |
| Ho002 | `host_require_config` passes when all three required vars are set |
| Ho003 | `host_confirm_gate` auto-confirms (no prompt) when `HARNESS_HOST_CONFIRM=1`, printing an "auto-confirmed" notice |
| Ho004 | `host_preflight` fails and names every missing host dependency (`python3`, `jq`, `node`, `opencode`) when they are absent from PATH |
| Ho005 | `host_write_opencode_config` emits valid JSON with `baseURL` pointed at `http://127.0.0.1:<port>/v1`, the `harness-dummy` placeholder apiKey, and the default model in the models map |
| Ho006 | `host_write_opencode_config` refuses (non-zero) when `jq` is absent rather than falling back to the docker jq sidecar (M4 guard) |
| Ho007 | `host_proxy_fingerprint` is stable across identical config and changes when `PROXY_PORT` or `PROXY_API_KEY` changes (drives config-change proxy restart, M2) |
| Ho008 | `harness upgrade` on a host-only install (no container runtime) takes a host-aware path: it does NOT abort on `require_docker` and does NOT rebuild images or restart a container stack, prints `[harness] host-only upgrade complete.`, and returns 0; a runtime-present install still goes through `require_docker` |
| Ho009 | `host_jq_platform`/`host_node_platform` produce well-formed, arch-consistent os-arch tokens for the running host (jq `linux/macos-amd64/arm64`, node `linux/darwin-x64/arm64`) |
| Ho010 | `host_sha_from_manifest` extracts the correct hash from a `<sha256>  <filename>` manifest and is end-anchored (no match for an absent or prefix-only name) |
| Ho011 | `host_sha256_check` accepts a file's real sha256 and rejects a wrong one |
| Ho012 | `host_toolchain_path_prefix` is empty when nothing is vendored and lists exactly the vendored jq, Node, and opencode bin dirs (in that order) once they exist |
| Ho013 | `host_ensure_toolchain` refuses on an unsupported host (not Linux/macOS/Windows) before downloading and, on a supported host, prepends the vendored toolchain dirs to `PATH` |
| Ho014 | host toolchain version pins (`HARNESS_HOST_OPENCODE_VERSION`, `HARNESS_HOST_OPENAI_COMPAT_VERSION`, `HARNESS_HOST_NODE_VERSION` major) stay in sync with `agents/Dockerfile` (drift guard) |
| Ho015 | Windows (Git Bash) toolchain layout: jq=`windows-amd64` exe, node=`win-x64`, `.exe` suffix, `node.exe`/opencode shim at dir root (not `bin/`), venv at `Scripts/python.exe`; win-arm64 resolves Node but jq fails closed (no upstream build) |
| Ho016 | `host_toolchain_path_prefix` on Windows orders the dirs `tool_bin:node-root:opencode-root` (root-level layout) |
| Ho017 | `host_extract_archive` extracts `.tar.gz` (and `.zip` when `zip`/`unzip` available) and rejects an unknown archive kind |
| Ho018 | `host_python_bin` verifies each candidate by running `--version` (not `command -v` alone): rejects a dead `python3` App-execution-alias stub and falls through to a real `python`; prefers a real `python3`; returns non-zero when only stubs exist |

## ChatGPT backend — CLI side (C###)

Behaviors of the `harness chatgpt` path: backend selection (`backend_override`),
the per-backend config/fingerprint/dispatch helpers it threads through in the
`harness` wrapper. Docker-free, sourced via `HARNESS_SOURCE_ONLY=1`. (The
proxy-side ChatGPT backend-api translation lives under P065–P081.)

| ID | Behavior |
|----|----------|
| C001 | `_backend_is_chatgpt` is false with no `backend_override` set (the default/openai backend) |
| C002 | `backend_override=chatgpt` makes `_backend_is_chatgpt` true |
| C003 | `_effective_default_model` returns `DEFAULT_MODEL_NAME` for the default backend and `CHATGPT_MODEL_NAME` for the chatgpt backend |
| C004 | `write_runtime_override` with `backend_override=chatgpt` injects `PROXY_BACKEND: "chatgpt"` onto the `proxy:` service block of the runtime override |
| C005 | The backend and prompt-mode overrides share a single `proxy:` mapping in the runtime override — no duplicate top-level `proxy:` keys (duplicate keys are invalid compose YAML) |
| C006 | `write_runtime_override` with neither `backend_override` nor `prompt_mode_override` active leaves no runtime override file behind |
| C007 | `require_runtime_config` swaps the required-var set to `CHATGPT_BASE_URL`/`CHATGPT_MODEL_NAME`/`CHATGPT_COOKIE_STRING` when `backend_override=chatgpt`, and does NOT require the openai `PROXY_API_URL`/`PROXY_API_KEY`/`DEFAULT_MODEL_NAME` trio |
| C008 | `require_runtime_config` does NOT require the `CHATGPT_*` trio for the default backend |
| C009 | `host_require_config` (host mode's lighter sibling of `require_runtime_config`) applies the same per-backend required-var swap |
| C010 | `_gate_on_upstream_auth` no-ops for the chatgpt backend (cookie auth has no bearer-key probe) |
| C011 | `_print_upstream_models` no-ops for the chatgpt backend (the backend-api has no model catalog to pull) |
| C012 | `host_proxy_fingerprint` changes when the backend switches, so a running host proxy serving the other backend is detected as stale |
| C013 | `host_proxy_fingerprint` changes when any of the three `CHATGPT_*` values changes (each is part of the fingerprint) |
| C014 | `_running_proxy_backend` reads `PROXY_BACKEND` from the running proxy container's env, reporting the `openai` default when the var is absent from an otherwise-running container |
| C015 | `_running_proxy_backend` fails/reports nothing when no proxy container is running |
| C016 | `ensure_services_up` restarts the stack when the running proxy serves a different backend than the one requested for this launch |
| C017 | `ensure_services_up` does NOT restart the stack when the running proxy already serves the requested backend |
| C018 | `_config_is_secret CHATGPT_COOKIE_STRING` classifies the cookie as a secret; `_config_get`/`_config_read_key` never print it in the clear (masked as `set, ...`) |
| C019 | `_config_editable_keys` lists `CHATGPT_BASE_URL`, `CHATGPT_MODEL_NAME`, and `CHATGPT_COOKIE_STRING` in the config picker |
| C020 | `_config_set CHATGPT_BASE_URL <url>` writes the value AND syncs the egress allowlist with the new host (so the firewall permits it) |
| C021 | `cmd_chatgpt --help` documents both the bare and `host` forms and the `CHATGPT_*` config keys, without selecting the backend (side-effect free) |
| C022 | `cmd_chatgpt` (bare form) selects the chatgpt backend, sets `agent_model` from `CHATGPT_MODEL_NAME`, and launches an agent |
| C023 | `cmd_chatgpt host …` selects the chatgpt backend and delegates the remaining args to `cmd_host` |
| C024 | `harness chatgpt` is wired into `main()`'s top-level command dispatch |
| C025 | `harness chatgpt [host] [args]` is documented in `cmd_help` |
| C026 | `_config_write_key` quotes any value that is not a bare word, so a `CHATGPT_COOKIE_STRING` carrying `; ` separators round-trips and the rewritten `.env` still sources cleanly under `set -euo pipefail`; bare values stay unquoted |
| C027 | `_config_write_key` prefers SINGLE quotes for a non-bare-word value (a single-quoted value is literal to both bash and compose's dotenv parser — no escape processing, no `${...}` interpolation), and falls back to double quotes plus backslash-escaping of backslash, `$`, backtick and `"` only when the value itself contains a single quote |
| C028 | `_config_read_key` is the exact inverse of `_config_write_key`: it strips ONE matched surrounding quote pair, unwrapping a single-quoted value literally and undoing the backslash, `$`, backtick and `"` escapes for the double-quoted form (the old blanket quote-strip silently corrupted any value containing `$`, a backtick, or a backslash) |
| C029 | `_chatgpt_config_fingerprint` hashes the three `CHATGPT_*` values, and `write_runtime_override` injects that hash as `HARNESS_PROXY_FP` on the `proxy:` service block — only the hash crosses the boundary, the cookie itself never appears in the on-disk compose override |
| C030 | `_running_proxy_fp` reads `HARNESS_PROXY_FP` back out of the running proxy container's env (empty when the var is absent) and does not surface the cookie sitting next to it in that env |
| C031 | `ensure_services_up` restarts the proxy when the chatgpt credentials changed (rotated cookie → fingerprint mismatch) even though the running backend already matches the requested one — compose will not replace a running container over an `.env` edit |
| C032 | `ensure_services_up` does NOT restart the openai backend on a missing fingerprint (the fingerprint check is gated on `_backend_is_chatgpt`, so an openai container that carries no `HARNESS_PROXY_FP` is left alone) |
| C033 | `cmd_chatgpt doctor` and `cmd_chatgpt preflight` delegate to `cmd_doctor`/`cmd_preflight` with `backend_override=chatgpt` already set, so the diagnostics check this backend's vars instead of reporting a valid chatgpt-only install as broken |
| C034 | `_print_upstream_models` DOES reach the upstream catalog request on the openai backend — the positive control that proves C011's chatgpt early-return is guarding a live code path, not a dead one |
| C035 | `cmd_chatgpt` routes the stack-lifecycle verbs `start` / `restart` / `down` to `cmd_start` / `cmd_restart` / `cmd_down` with `backend_override=chatgpt` already set — without those labels they fall through to `*)` and launch an agent with a stray argument, and `cmd_start` aborts on the OpenAI trio |
| C036 | `_adopt_running_backend` adopts a RUNNING chatgpt proxy into `backend_override` (refreshing `agent_model`), leaves `openai` as the implicit empty default so `write_runtime_override`'s output stays byte-identical for pre-existing installs, never clobbers an explicit selection, and does not probe at all on an install with no chatgpt config |
| C037 | `cmd_restart` calls `_adopt_running_backend` BEFORE `cmd_down` — the dialect is read out of the container that the teardown is about to destroy — so a chatgpt-only install restarts onto chatgpt instead of dying in `require_runtime_config` on the OpenAI trio |
| C038 | `ensure_services_up` skips the whole backend/fingerprint reconciliation block (`_chatgpt_in_play`) on an install that never configured `CHATGPT_BASE_URL`: the probes cost a `compose ps` plus a `docker inspect` each, and an install with no chatgpt config cannot have a chatgpt proxy running. It resumes when the key is configured OR when this launch explicitly selects the backend |
| C039 | bare `cmd_preflight` on a chatgpt-only `.env` prints a pointer to `harness chatgpt preflight` alongside the three OpenAI-key failures, and does NOT print it when the OpenAI keys are the ones configured |
| C040 | `harness chatgpt doctor` fails the `CHATGPT_COOKIE_STRING` line when the sourced value is a strict prefix of the value `_config_read_key` reads off disk (`_config_value_truncated`), reporting `truncated: N of M chars` plus the fix — the unquoted-`.env` fault previously rendered as a green `set, N chars` with the wrong N. Identical values, a non-prefix difference, and either side empty are all left alone |
