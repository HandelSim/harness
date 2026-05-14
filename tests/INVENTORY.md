# Harness — Testable Behavior Inventory

This document enumerates every atomic, testable behavior of the harness project. Each row is one behavior with a stable ID. IDs are grouped by prefix:

- **F###** — CLI commands and flags (the `harness` wrapper)
- **P###** — Proxy translation behaviors (`proxy/proxy.py`)
- **A###** — Agent entrypoint (`agents/entrypoint.sh`)
- **M###** — MCP lifecycle (`harness mcp …`, compose, registry state)
- **N###** — Firewall guardrails (`firewall/init-firewall.sh`, network overrides)
- **U###** — Upgrade actions (`scripts/lib/upgrade_actions.sh`, manifest dispatch)
- **Pe###** — Persistence (where state lives, what survives, what is regenerated)
- **O###** — Ollama stub model (`ollama/entrypoint.sh`, model registration)
- **I###** — Installer (`harness-install.sh`)

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
| F023 | `harness start` launches the `ollama` and `proxy` services via `docker compose up -d` |
| F024 | `harness start` aborts when `require_runtime_config` fails |
| F025 | `harness start` invokes `warn_if_firewall_open` and prints a banner when any host is in net-overrides |
| F026 | `harness start` writes a runtime override file (`state/.harness-runtime.yml`) before invoking compose |
| F027 | `harness start` gates on `_probe_upstream_auth` and prints unlock URL when upstream returns 401 |
| F028 | `harness down` runs `docker compose down` for the harness project |
| F029 | `harness down` does NOT remove user state under `state/agent/home` or `state/ollama-data` |
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
| F041 | `harness logs <service>` follows compose logs for the named service |
| F042 | `harness logs` (no service) tails all services |
| F043 | `harness claude` launches the agent container with the claude profile and `claude` mode |
| F044 | `harness opencode` launches the agent container with the `opencode` mode |
| F045 | `harness shell` launches the agent container with the `shell` mode and an interactive TTY |
| F046 | `harness claude --yolo` sets `HARNESS_YOLO=1` in the agent container env |
| F047 | `harness claude --net <svc>` enables the named net-override service for this invocation |
| F048 | `harness claude --mount <path>` adds a bind mount to the agent container |
| F049 | `harness claude --mount` rejects unsafe targets via `harness_validate_mount` |
| F050 | `--mount` is repeatable: multiple `--mount <path>` flags accumulate |
| F051 | `harness claude -p "<prompt>"` runs claude in print mode |
| F052 | `harness claude --print "<prompt>"` is an alias for `-p` |
| F053 | `harness opencode -p "<prompt>"` runs opencode in print mode |
| F054 | `agent_container_name` derives a deterministic container name via sha256 of the install-root path |
| F055 | Two installs in different roots produce different agent container names |
| F056 | `harness list` lists currently running harness containers for this install root |
| F057 | `harness stop` stops the agent container without affecting `ollama`/`proxy` |
| F058 | `harness stop <name>` stops the named agent container |
| F059 | `pick_agent` prompts when multiple agents are running |
| F060 | `pick_agent` returns the single running agent without prompting |
| F061 | `pick_agent` errors when no agent is running |
| F062 | `harness claude-statusline-config` writes the ccstatusline config into the shared agent home |
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
| F090 | `harness unlock` prints the upstream unlock URL when probe returns 401 |
| F091 | `harness unlock` prints "already authorized" when probe returns 200 |
| F092 | `harness unlock` reports error when probe is unreachable |
| F093 | `_probe_upstream_auth` returns 0 on HTTP 200, 1 on 401/403, 2 on connection failure |
| F094 | `_gate_on_upstream_auth` runs the probe and aborts `start` on auth failure |
| F095 | `harness doctor` reports deps section: docker/podman + git + jq presence |
| F096 | `harness doctor` reports install section: install-root path, wrapper presence |
| F097 | `harness doctor` reports config section: `.env` and `.harness-allowlist` presence and parseability |
| F098 | `harness doctor` reports network section: PROXY_API_URL hostname allowlisted vs not |
| F099 | `harness doctor` reports storage section: state/output, state/agent/home, state/ollama-data writability |
| F100 | `harness doctor` reports runtime section: docker/podman daemon reachable |
| F101 | `harness doctor` reports images section: presence and age of `harness-proxy`, `harness-ollama`, `harness-agents` |
| F102 | `harness doctor` reports mcp section: installed MCP services and enabled/disabled state |
| F103 | `harness doctor` reports agents section: any agent container currently running |
| F104 | `harness preflight` validates required commands exist (docker/podman, git) |
| F105 | `harness preflight` validates `.env` exists with non-empty `PROXY_API_URL`, `PROXY_API_KEY`, `PROXY_API_MODEL` |
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
| P007 | Proxy reads `PROXY_API_MODEL` from env |
| P008 | Proxy reads `PROXY_TIMEOUT` from env |
| P009 | Proxy reads `OLLAMA_CONTEXT_LENGTH` from env |
| P010 | Proxy reads `PROXY_PROMPT_MODE` from env (default `user_front`) |
| P011 | Proxy reads `PROXY_CHANGE_SYSTEM_PROMPT_TO_USER` from env |
| P012 | Proxy reads `OUTPUT_DIR` from env for debug dumps |
| P013 | `PROXY_PROMPT_MODE=user_front` injects the cooperative prompt before the latest user message |
| P014 | `PROXY_PROMPT_MODE=user_bookend` injects cooperative prompt both before and after the latest user message |
| P015 | `PROXY_PROMPT_MODE=user` (legacy) appends cooperative prompt after the latest user message |
| P016 | `PROXY_PROMPT_MODE=system` appends cooperative addition to the upstream system message |
| P017 | `PROXY_PROMPT_MODE=hybrid` combines a system addition with a user-side reminder |
| P018 | Unknown `PROXY_PROMPT_MODE` falls back to `user_front` and warns |
| P019 | `extract_tool_calls_and_text` parses fenced ```json blocks from assistant text |
| P020 | `extract_tool_calls_and_text` extracts ALL `json` blocks, not just the first |
| P021 | `extract_tool_calls_and_text` preserves the textual order of tool_use blocks |
| P022 | `_scan_balanced_json` correctly handles strings containing escaped quotes |
| P023 | `_scan_balanced_json` correctly handles strings containing literal backticks |
| P024 | `_scan_balanced_json` returns failure on truncated JSON |
| P025 | Tool calls in assistant history are converted to fenced ```json markdown blocks for upstream |
| P026 | Tool results in user history are converted to `System Observation:` blocks for upstream |
| P027 | `translate_history_and_apply_prompt` coalesces consecutive same-role messages |
| P028 | Multiple consecutive system messages are merged into one |
| P029 | Multiple consecutive user messages are merged into one |
| P030 | When `PROXY_CHANGE_SYSTEM_PROMPT_TO_USER=1`, the system message is rewritten as a user message |
| P031 | When system→user rewrite happens, a stub assistant turn ("Understood…") is inserted so upstream sees user/assistant alternation |
| P032 | `make_chunk` emits ollama-shaped NDJSON chunks |
| P033 | Final NDJSON chunk includes `done: true` and `done_reason` |
| P034 | Final NDJSON chunk includes `usage` derived from upstream usage or fallback estimate |
| P035 | Tool-call ids emitted on the ollama side are prefixed `toolu_` and use a deterministic uuid format |
| P036 | `_estimate_tokens` returns `len(text)//4` capped at `OLLAMA_CONTEXT_LENGTH` |
| P037 | Upstream 401 response prints the unlock URL and forwards 401 |
| P038 | Upstream 403 response prints the unlock URL and forwards 403 |
| P039 | Upstream 429 response prints a rate-limit warning and forwards 429 |
| P040 | Upstream 5xx response prints a warning and forwards the status |
| P041 | Upstream connection failure surfaces as HTTP 502 to ollama |
| P042 | Upstream non-JSON response surfaces as HTTP 502 to ollama |
| P043 | When `OUTPUT_DIR` is set, request dumps go to `01_Ollama_Request_*` files |
| P044 | When `OUTPUT_DIR` is set, upstream request dumps go to `02_API_Request_*` files |
| P045 | When `OUTPUT_DIR` is set, upstream OK response dumps go to `03_API_Response_*` files |
| P046 | When `OUTPUT_DIR` is set, upstream error dumps go to `03_API_Error_*` files |
| P047 | When `OUTPUT_DIR` is set, ndjson output dumps go to `04_NDJSON_Response_*` files |
| P048 | When `OUTPUT_DIR` is set, fatal exceptions dump to `99_Fatal_Error_*` files |
| P049 | Debug dump filenames embed a monotonic counter and timestamp |
| P050 | Debug dumps are skipped silently when `OUTPUT_DIR` is unset |
| P051 | Streaming requests yield NDJSON with one chunk per upstream delta |
| P052 | Non-streaming requests still yield a single NDJSON line followed by the done chunk |
| P053 | Tool-result messages in the inbound ollama payload are translated into user-role `System Observation:` text |
| P054 | The cooperative prompt instructs the model to use ```json fenced blocks for tool calls |
| P055 | The cooperative prompt enumerates available tools by name and schema |
| P056 | Proxy passes through the upstream `model` field as `PROXY_API_MODEL`, not the inbound ollama model name |

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
| A011 | `ensure_claude_config` creates `~/.claude/settings.json` if missing |
| A012 | `ensure_claude_config` sets `includeCoAuthoredBy: false` in the settings |
| A013 | `ensure_claude_config` sets a `statusLine` entry pointing at ccstatusline |
| A014 | `ensure_claude_config` merges into an existing settings.json via `jq` rather than overwriting |
| A015 | `merge_claude_mcp_servers` reads `~/.harness-mcp-servers.json` |
| A016 | `merge_claude_mcp_servers` folds entries into `~/.claude.json` `mcpServers` key |
| A017 | `merge_claude_mcp_servers` overwrites entries with the same name (last write wins) |
| A018 | `ensure_opencode_config` writes `~/.config/opencode/opencode.json` on every launch |
| A019 | `ensure_opencode_config` configures a `harness` provider pointing at the local ollama endpoint |
| A020 | `ensure_opencode_config` defines a `yolo` agent profile |
| A021 | `merge_opencode_mcp_servers` translates the claude-shaped mcp-servers JSON into opencode's mcp shape |
| A022 | `merge_opencode_mcp_servers` distinguishes `local` (stdio) from `remote` (SSE/HTTP) entries |
| A023 | `run_claude` requires `ANTHROPIC_BASE_URL` to be set, errors otherwise |
| A024 | `run_claude` sets `ANTHROPIC_AUTH_TOKEN=harness-dummy` |
| A025 | `run_claude` sets `DISABLE_AUTOUPDATER=1` |
| A026 | `run_claude` passes `--dangerously-skip-permissions` when `HARNESS_YOLO=1` |
| A027 | `run_claude` passes `-p "$prompt"` straight through to the claude CLI |
| A028 | `run_opencode` sets `OPENCODE_DISABLE_AUTOUPDATE=1` |
| A029 | `run_opencode` passes `--agent yolo` when `HARNESS_YOLO=1` |
| A030 | `run_opencode` invokes `opencode run "<prompt>"` when `-p` is passed |
| A031 | `run_opencode` strips the `-p`/`--print` flag before forwarding remaining args |
| A032 | `run_shell` execs `bash -l` so login dotfiles run |
| A033 | Entry dispatch chooses claude/opencode/shell based on the first positional arg |
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

---

## Persistence (Pe###)

| ID | Behavior |
|----|----------|
| Pe001 | `.env` (user config) lives at install-root and survives `down`/`restart`/`upgrade` |
| Pe002 | `.harness-allowlist` (user config) lives at install-root and survives `down`/`restart`/`upgrade` |
| Pe003 | `.harness-net-overrides.json` lives at install-root and survives `down` |
| Pe004 | `state/agent/home/` is a per-install shared agent home, persisted across agent runs |
| Pe005 | `state/ollama-data/` holds the stub model registration data and survives container restarts |
| Pe006 | `state/output/` is the proxy debug-dump directory; never managed by upgrades |
| Pe007 | `state/mcp/<name>/` holds installed MCP service state |
| Pe008 | `state/mcp/<name>/harness-meta.json` holds enabled/disabled state and is preserved by `directory_overwrite` upgrades |
| Pe009 | `state/mcp/serena/data/` (Serena index) is preserved by upgrades |
| Pe010 | `state/.harness-runtime.yml` is regenerated on every compose invocation |
| Pe011 | `state/agent/home/.harness-mcp-servers.json` is regenerated on every agent launch |
| Pe012 | `state/agent/home/.claude/settings.json` is auto-augmented on every agent launch (statusLine + includeCoAuthoredBy) |
| Pe013 | `state/agent/home/.config/ccstatusline/settings.json` is seeded once from `/etc/skel` then user-managed |
| Pe014 | `state/agent/home/.bashrc`, `.gitconfig` etc are seeded once from `/etc/skel/harness/.` then user-managed |
| Pe015 | `harness down` does not delete any `state/` content |
| Pe016 | `harness upgrade` does not delete any `state/` content |
| Pe017 | `harness mcp uninstall <name>` deletes `state/mcp/<name>/` (the only path that removes state) |
| Pe018 | `harness-meta.json` survives `directory_overwrite` even when the registry source no longer ships one |
| Pe019 | `data/` subdirs of MCP installs survive `directory_overwrite` via the manifest `preserve` list |

---

## Ollama stub model (O###)

| ID | Behavior |
|----|----------|
| O001 | `ollama/entrypoint.sh` runs `init-firewall.sh` before starting ollama |
| O002 | `ollama serve` is launched in the background and its PID captured |
| O003 | Entrypoint polls `/api/tags` up to 60 seconds for ollama readiness |
| O004 | Entrypoint fatally errors if ollama is not ready within 60 seconds |
| O005 | `register_stub_model` POSTs `/api/create` with `model`, `from`, `remote_host` fields |
| O006 | `register_stub_model` POSTs `info.context_length` matching `OLLAMA_CONTEXT_LENGTH` |
| O007 | `register_stub_model` POSTs `parameters.num_ctx` matching `OLLAMA_CONTEXT_LENGTH` |
| O008 | `register_stub_model` requires the response stream's final line to have `"status":"success"` |
| O009 | Canonical `MODEL_NAME` registration is fatal on failure (exit non-zero) |
| O010 | Alias `sonnet` registration is best-effort (failure is logged, not fatal) |
| O011 | Alias `opus` registration is best-effort |
| O012 | Alias `haiku` registration is best-effort |
| O013 | Alias `claude-sonnet-4-5` registration is best-effort |
| O014 | Alias `claude-opus-4-5` registration is best-effort |
| O015 | Alias `claude-haiku-4-5` registration is best-effort |
| O016 | Alias `claude-3-5-sonnet-20241022` registration is best-effort |
| O017 | Alias `claude-3-5-haiku-20241022` registration is best-effort |
| O018 | Alias `claude-3-opus-20240229` registration is best-effort |
| O019 | All registered aliases point at the proxy via `remote_host` |
| O020 | Trap on EXIT cleans up the background ollama process |
| O021 | Trap on INT cleans up the background ollama process |
| O022 | Trap on TERM cleans up the background ollama process |
| O023 | `OLLAMA_REMOTES=proxy` ensures ollama trusts the proxy as remote host |
| O024 | Ollama healthcheck probes `/api/tags` and reports healthy when registered |
| O025 | Ollama service `depends_on: proxy: service_healthy` blocks startup until proxy is healthy |

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
| I016 | The installer creates `state/ollama-data/` |
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
