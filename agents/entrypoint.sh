#!/usr/bin/env bash
#
# harness-agent — unified entrypoint dispatching on a mode argument:
#
#   opencode  — run opencode
#   shell     — drop into bash inside the container (for installing skills,
#                debugging, etc.)
#
# All shared infrastructure (UID remap, firewall, gosu drop, skel seed, git
# config) runs once at the top, regardless of mode; mode dispatch happens
# after privilege drop.
#
# Common harness-level controls:
#   HARNESS_YOLO=1              — opencode: --agent yolo
#   HARNESS_PRINT_MODE=1        — `harness -p ...` headless single-shot
#   HARNESS_HOST_CWD=<path>     — host CWD; the harness CLI bind-mounts the
#                                 host CWD at this same absolute path inside
#                                 the container, and the entrypoint cd's
#                                 here so PWD matches the host path
#                                 (e.g. /c/Users/you/projects/myapp).
#   HARNESS_FIREWALL_DISABLED=1 — skip init-firewall.sh entirely
#                                 (--net flag or `harness net open`)

set -euo pipefail

# --- root-side init: firewall + UID remap + gosu drop -----------------------
#
# When the harness script invokes with `docker run --user 0:0`, we land here
# as root. We always drop to the `harness` user before running any agent
# code; the remap to match the host caller's uid/gid is conditional on
# HOST_UID/HOST_GID being set.
#
# Why drop unconditionally: agents should never run as root, and we want
# test invocations (which don't pass --user 0:0 — they let docker default
# and would land as root because the image has no `USER` directive) to
# behave the same as production launches w.r.t. user identity.
if [[ "$(id -u)" == "0" ]]; then
    # Lay down the egress firewall before dropping privileges
    # (iptables/ipset need NET_ADMIN/NET_RAW which gosu does NOT preserve
    # when stepping down to a non-zero uid). Skipped if explicitly opted
    # out via HARNESS_FIREWALL_DISABLED=1.
    if [[ "${HARNESS_FIREWALL_DISABLED:-0}" != "1" ]]; then
        if [[ -x /usr/local/bin/init-firewall.sh ]]; then
            /usr/local/bin/init-firewall.sh \
                || echo "[agent-entrypoint] WARN: init-firewall.sh failed; continuing without firewall" >&2
        else
            echo "[agent-entrypoint] WARN: init-firewall.sh missing; running without firewall" >&2
        fi
    fi

    # UID remap is only requested when the harness script set HOST_UID/GID.
    # Test scripts that don't pass --user 0:0 may still land here (because
    # of the no-USER-directive choice); they don't need the remap.
    if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
        current_uid=$(id -u harness 2>/dev/null || echo "")
        current_gid=$(id -g harness 2>/dev/null || echo "")
        if [[ "${current_uid}" != "${HOST_UID}" || "${current_gid}" != "${HOST_GID}" ]]; then
            # -o allows duplicate ids — defends against hosts where uid
            # 0/whatever is already claimed by another account.
            groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
                || groupadd -g "${HOST_GID}" -o harness
            usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness
            # Skip recursive chown if the home dir's top-level ownership is
            # already correct. On Windows + Docker Desktop, recursive chown
            # across a bind-mounted host path is dramatically slow (every
            # syscall is a WSL2/virtiofs translation to the Windows
            # filesystem). After the first launch chowns the tree, subsequent
            # launches see correct ownership and can skip the walk.
            existing_uid=$(stat -c '%u' /home/harness 2>/dev/null || echo "")
            if [[ "$existing_uid" != "${HOST_UID}" ]]; then
                chown -R "${HOST_UID}:${HOST_GID}" /home/harness 2>/dev/null || true
            fi
        fi
    fi

    exec gosu harness "$0" "$@"
fi

# --- We're now running as the harness user ----------------------------------

# --- git credentials (user-side) --------------------------------------------
#
# Runs after the gosu drop so `git config --global` writes to
# /home/harness/.gitconfig (not /root/.gitconfig). Best-effort: a missing
# allowlist is unusual — the firewall would have already failed — but we
# don't want to make the agent itself unstartable on credential setup
# hiccups.
if [[ -x /usr/local/bin/configure-git-credentials.sh ]]; then
    /usr/local/bin/configure-git-credentials.sh /etc/harness/allowlist \
        || echo "[agent-entrypoint] WARN: configure-git-credentials.sh failed; git push protection may be incomplete" >&2
fi

# --- skel seed --------------------------------------------------------------
#
# The harness script bind-mounts <install-root>/state/agent/home over
# /home/harness, so on first run the home dir is empty (or only contains
# the user's bring-along files). Restore the build-time skeleton (~/.bashrc,
# pipx's data dir layout, etc.) once, marked by
# ~/.harness-home-initialized. `cp -an` is "archive + no-clobber" so any
# file the user already placed in the bind mount wins. Failures on
# individual files (perms quirks) shouldn't abort the agent — hence
# the `|| true`.
if [[ ! -f "${HOME}/.harness-home-initialized" ]]; then
    if [[ -d /etc/skel/harness ]]; then
        cp -an /etc/skel/harness/. "${HOME}/" 2>/dev/null || true
    fi
    touch "${HOME}/.harness-home-initialized" 2>/dev/null || true
fi

# --- change into host CWD path ----------------------------------------------
#
# The harness CLI bind-mounted the host CWD at this same absolute path and
# set --workdir to it; cd is mostly a belt-and-braces since gosu's re-exec
# inherits cwd, but it ensures any direct test invocations (which may not
# pass --workdir) still land here. Failure is non-fatal: if HARNESS_HOST_CWD
# isn't a real directory we just stay in whatever cwd we inherited.
if [[ -n "${HARNESS_HOST_CWD:-}" && -d "${HARNESS_HOST_CWD}" ]]; then
    cd "${HARNESS_HOST_CWD}" || true
fi

# --- mode dispatch ----------------------------------------------------------

mode="${1:-opencode}"
shift || true

# --- helpers shared across modes --------------------------------------------

ensure_opencode_config() {
    local config_dir="${HOME}/.config/opencode"
    local config_file="${config_dir}/opencode.json"
    mkdir -p "$config_dir"

    local default_model="${DEFAULT_MODEL_NAME:-}"
    local provider_name="GenAI Harness"
    local ollama_url="http://ollama:11434/v1"
    local ctx="${OLLAMA_CONTEXT_LENGTH:-200000}"

    # Build the opencode model dropdown from the stubs ollama actually has
    # registered (its /api/tags), so opencode lists exactly the upstream
    # models discovered at start — and never drifts from what's registered.
    # ollama tags stubs as `<id>:latest`; strip the tag back to the bare
    # upstream id opencode/the proxy use. Falls back to DEFAULT_MODEL_NAME if
    # ollama isn't reachable yet so the config always has at least one model.
    local model_ids=()
    local tags
    tags=$(curl -fsS --max-time 10 "http://ollama:11434/api/tags" 2>/dev/null || true)
    if [[ -n "$tags" ]]; then
        mapfile -t model_ids < <(printf '%s' "$tags" \
            | jq -r '.models[]?.name | sub(":latest$"; "")' 2>/dev/null | sort -u || true)
    fi
    # Ensure the default model is always present (it's the default selection).
    if [[ -n "$default_model" ]]; then
        local found=0 m
        for m in "${model_ids[@]}"; do [[ "$m" == "$default_model" ]] && found=1; done
        (( found == 0 )) && model_ids+=("$default_model")
    fi
    if (( ${#model_ids[@]} == 0 )); then
        model_ids=("${default_model:-default}")
    fi

    # Selected model: the configured default if present, else the first id.
    local selected="$default_model"
    if [[ -z "$selected" ]]; then
        selected="${model_ids[0]}"
    fi

    # provider.models map: {"<id>": {"name": "<id>", "limit": {...}}, ...}
    local models_json
    models_json=$(printf '%s\n' "${model_ids[@]}" | jq -R . | jq -s \
        --argjson ctx "$ctx" \
        'map({key: ., value: {name: ., limit: {context: $ctx, output: 8192}}}) | from_entries')

    # Always (re)write the harness-managed block — the model set may have
    # changed between launches. Built with jq so the models map is dynamic and
    # the strings are correctly escaped.
    jq -n \
        --arg provider_name "$provider_name" \
        --arg ollama_url "$ollama_url" \
        --arg selected "harness/${selected}" \
        --argjson models "$models_json" \
        '{
          "$schema": "https://opencode.ai/config.json",
          "provider": {
            "harness": {
              "npm": "@ai-sdk/openai-compatible",
              "name": $provider_name,
              "options": {"baseURL": $ollama_url, "apiKey": "harness-dummy"},
              "models": $models
            }
          },
          "model": $selected,
          "small_model": $selected,
          "agent": {
            "yolo": {
              "description": "Auto-approve all permissions; harness yolo mode",
              "permission": {"edit": "allow", "bash": {"*": "allow"}, "webfetch": "allow", "websearch": "allow"}
            }
          }
        }' > "$config_file"
}

merge_opencode_mcp_servers() {
    # The harness script writes ~/.harness-mcp-servers.json in the canonical
    # `{"mcpServers": {...}}` shape. Opencode expects an `mcp` top-level block
    # with a different per-entry shape:
    #   {"mcp": {"<name>": {"type": "remote", "url": "..."}}} for HTTP/SSE
    #   {"mcp": {"<name>": {"type": "local", "command": [...]}}} for stdio
    # Translate inline so the host harness script stays agent-agnostic.
    if [[ ! -f "${HOME}/.harness-mcp-servers.json" ]] || ! command -v jq >/dev/null 2>&1; then
        return 0
    fi
    local config_file="${HOME}/.config/opencode/opencode.json"
    [[ -f "$config_file" ]] || return 0

    local merged
    merged=$(jq -s '
        .[0] as $cfg
        | .[1] as $harness
        | ($harness.mcpServers // {}) as $servers
        | $servers
        | to_entries
        | map(
            .value as $v
            | if ($v.command // null) != null then
                  {key: .key, value: {type: "local", command: ([$v.command] + ($v.args // []))}}
              else
                  {key: .key, value: {type: "remote", url: ($v.url // "")}}
              end
          )
        | from_entries
        | . as $opencode_mcp
        | $cfg | .mcp = ((.mcp // {}) + $opencode_mcp)
    ' "$config_file" "${HOME}/.harness-mcp-servers.json" 2>/dev/null || true)
    if [[ -n "${merged}" ]]; then
        printf '%s\n' "${merged}" > "$config_file"
    fi
}

# --- mode: opencode --------------------------------------------------------

run_opencode() {
    ensure_opencode_config
    merge_opencode_mcp_servers

    export OPENCODE_DISABLE_AUTOUPDATE=1
    # Expose opencode's built-in `websearch` tool to the model. Opencode hides
    # it by default unless either (a) the official OpenCode provider is in use
    # — we use the openai-compatible adapter pointing at harness's proxy, so
    # this branch never fires for us — or (b) OPENCODE_ENABLE_EXA is set. With
    # this on, websearch ships in the tool list every turn; the actual call
    # hits Exa's hosted MCP at mcp.exa.ai, which the egress firewall blocks
    # by design (same firewall-down / --net use case as the rest of
    # unrestricted egress).
    #
    # Reachability gate (firewall-down only). Opencode's MCP startup is
    # synchronous (anomalyco/opencode#20755) — it blocks the TUI on the
    # `initialize` + `list_tools` round-trip to mcp.exa.ai. With the
    # firewall UP, the TCP REJECT is instant so the handshake fails fast
    # and the var is harmless (websearch surfaces, calls fail at use
    # time — the right failure mode). With the firewall DOWN (`--net`),
    # the handshake actually runs against Cloudflare-fronted Exa, which
    # is intermittently slow / returns 520s (anomalyco/opencode#6878) —
    # then the TUI never appears. Probe Exa before exporting the var:
    # if it doesn't answer within ~1.5s, leave the var unset so opencode
    # skips Exa registration entirely and the TUI starts immediately.
    # Net effect: websearch ships when Exa is actually reachable, and
    # opencode never gets stuck waiting for a sick Exa endpoint.
    if [[ "${HARNESS_FIREWALL_DISABLED:-0}" == "1" ]]; then
        if command -v curl >/dev/null 2>&1 \
            && curl --connect-timeout 1 --max-time 2 -so /dev/null \
                    https://mcp.exa.ai/mcp 2>/dev/null
        then
            export OPENCODE_ENABLE_EXA=1
        else
            echo "[agent-entrypoint] mcp.exa.ai unreachable (or slow) — skipping OPENCODE_ENABLE_EXA so the TUI doesn't hang on Exa's MCP handshake" >&2
        fi
    else
        # Firewall up: REJECT is instant; opencode fails fast on Exa
        # without us probing. Keep the var on so the tool surface is
        # consistent with the firewall-down case for the prompt builder
        # (recency, etc.) — invocations just return a network error.
        export OPENCODE_ENABLE_EXA=1
    fi

    echo "============================================================"
    echo " harness-agent (opencode)"
    echo "   provider: GenAI Harness"
    echo "   model:    harness/${DEFAULT_MODEL_NAME:-default}"
    echo "   ollama:   http://ollama:11434/v1"
    echo "   yolo:     ${HARNESS_YOLO:-0}"
    echo "   print:    ${HARNESS_PRINT_MODE:-0}"
    echo "============================================================"

    local args=()
    if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
        args+=(--agent yolo)
    fi

    if [[ "${HARNESS_PRINT_MODE:-0}" == "1" ]]; then
        # opencode has no `-p` flag — strip a leading -p / --print if the
        # harness forwarded it, hand the rest to `opencode run`.
        local op_args=()
        local seen_p=0
        local arg
        for arg in "$@"; do
            if [[ "$seen_p" == "0" && ("$arg" == "-p" || "$arg" == "--print") ]]; then
                seen_p=1
                continue
            fi
            op_args+=("$arg")
        done

        # opencode 1.15.x's `run` renderer drops the assistant body on a
        # non-TTY stdout when the reply finalizes before the session goes idle
        # (fast/short responses lose that race — see the note in Dockerfile and
        # architecture/containers.md). `--format json` doesn't dodge it either.
        # So we don't trust run's stdout for headless `-p`: capture the json
        # event stream to learn the session id, then print the final assistant
        # text from the persisted session via `opencode export`, which is
        # independent of the render race. stderr flows through so opencode's
        # own diagnostics (e.g. provider-auth errors the tests skip on) stay
        # visible, and the run's exit code is preserved.
        local events_file rc sid text
        events_file="$(mktemp)"
        # `|| rc=$?` keeps set -e from aborting on a non-zero opencode exit and
        # preserves the code so the tests' provider-auth skip path still works.
        rc=0
        opencode run --format json "${args[@]}" "${op_args[@]}" >"${events_file}" || rc=$?
        # Learn the session id. Prefer the json event stream — it names *this*
        # run's session — but opencode 1.15.x intermittently writes an empty
        # stream to a non-TTY stdout (the same finalize-before-idle race that
        # drops the body; ~10% of fast runs emit zero bytes, not even the
        # step_start that carries the id). When the stream gives us nothing,
        # fall back to the newest persisted session for this directory via
        # `opencode session list`, which reads the session store directly and
        # is independent of the render race.
        sid="$(jq -rs 'map(select(.sessionID))[0].sessionID // empty' "${events_file}" 2>/dev/null || true)"
        if [[ -z "${sid}" ]]; then
            sid="$(opencode session list --format json 2>/dev/null \
                | jq -r --arg dir "$PWD" '((map(select(.directory==$dir)) | max_by(.created) | .id) // (max_by(.created) | .id)) // empty' 2>/dev/null || true)"
        fi
        text=""
        if [[ -n "${sid}" ]]; then
            text="$(opencode export "${sid}" 2>/dev/null \
                | jq -r '([.messages[] | select(.info.role=="assistant")] | last | (.parts[]? | select(.type=="text") | .text)) // empty' 2>/dev/null || true)"
        fi
        # Last-ditch: if export yielded nothing but the json stream happened to
        # carry text parts, use them.
        if [[ -z "${text}" ]]; then
            text="$(jq -rs '[.[] | select(.type=="text") | .part.text] | join("")' "${events_file}" 2>/dev/null || true)"
        fi
        if [[ -n "${text}" ]]; then
            printf '%s\n' "${text}"
        fi
        rm -f "${events_file}"
        exit "${rc}"
    fi

    # Always foreground exec — no tmux. The container's PID 1 becomes opencode
    # itself, so the user's terminal connects directly to its PTY and the
    # container exits when opencode exits.
    exec opencode "${args[@]}" "$@"
}

# --- mode: shell -----------------------------------------------------------

run_shell() {
    echo "============================================================"
    echo " harness-agent (shell)"
    echo "   workspace: $(pwd)"
    echo "   home:      ${HOME}"
    echo "============================================================"
    echo
    echo "Drop into an interactive bash inside the agent container. Exit"
    echo "with 'exit' or Ctrl+D. The home directory is shared across all"
    echo "agent modes, so installs (pipx, etc.) persist."
    echo
    exec bash -l
}

# --- dispatch --------------------------------------------------------------

case "$mode" in
    opencode)
        run_opencode "$@"
        ;;
    shell)
        run_shell
        ;;
    *)
        echo "[agent-entrypoint] unknown mode: $mode" >&2
        echo "[agent-entrypoint] valid modes: opencode, shell" >&2
        exit 1
        ;;
esac
